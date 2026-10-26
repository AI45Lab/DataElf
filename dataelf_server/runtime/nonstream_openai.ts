import { stripThinkingPrefix } from "./thinking.ts";
import { ToolEnvelopeError, parseTextualToolCalls, decodeTextualTools, validateNativeArguments, type OfferedTools } from "./textual_tools.ts";
export { parseTextualToolCalls } from "./textual_tools.ts";
import { spawn } from "node:child_process";
import { appendFile } from "node:fs/promises";
import {
	type AssistantMessage,
	type AssistantMessageEventStream,
	type Context,
	createAssistantMessageEventStream,
	type Model,
	type SimpleStreamOptions,
	type StopReason,
	type ToolCall,
} from "@earendil-works/pi-ai";

type JsonRecord = Record<string, unknown>;
type AssistantStreamFactory = () => AssistantMessageEventStream | Promise<AssistantMessageEventStream>;

interface RetryingStreamOptions {
	maxRetries: number;
	eventLogPath?: string;
	label?: string;
	retryDelayMs?: number;
	signal?: AbortSignal;
}

let streamingRequestSequence = 0;

interface ChatCompletionEnvelope {
	id?: string;
	model?: string;
	choices?: Array<{
		finish_reason?: string | null;
		message?: {
			content?: string | null;
			tool_calls?: OpenAIToolCall[];
		};
	}>;
	usage?: {
		prompt_tokens?: number;
		completion_tokens?: number;
		total_tokens?: number;
		prompt_tokens_details?: { cached_tokens?: number };
	};
}

interface OpenAIToolCall {
	id?: string;
	type?: string;
	function?: { name?: string; arguments?: string };
}

interface TextualToolCallParseResult {
	matched: boolean;
	toolCalls: OpenAIToolCall[];
}

/**
 * Some OpenAI-compatible GLM deployments serialize a forced function call in
 * the assistant content instead of returning `message.tool_calls`.  Accept
 * only a tool-only response with the gateway's exact tag structure; ordinary
 * prose containing a tag remains plain text and can never become executable.
 */

function contentText(content: unknown): string {
	if (typeof content === "string") return content;
	if (!Array.isArray(content)) return "";
	return content
		.map((block) => {
			if (!block || typeof block !== "object") return "";
			const value = block as JsonRecord;
			return value.type === "text" && typeof value.text === "string" ? value.text : "";
		})
		.filter(Boolean)
		.join("\n");
}

function openAIMessages(context: Context): JsonRecord[] {
	const messages: JsonRecord[] = [];
	if (context.systemPrompt?.trim()) messages.push({ role: "system", content: context.systemPrompt });
	for (const message of context.messages) {
		if (message.role === "user") {
			messages.push({ role: "user", content: contentText(message.content) });
			continue;
		}
		if (message.role === "toolResult") {
			messages.push({
				role: "tool",
				tool_call_id: message.toolCallId,
				name: message.toolName,
				content: contentText(message.content),
			});
			continue;
		}
		const text = message.content
			.filter((block) => block.type === "text")
			.map((block) => block.text)
			.join("\n");
		const toolCalls = message.content
			.filter((block): block is ToolCall => block.type === "toolCall")
			.map((block) => ({
				id: block.id,
				type: "function",
				function: { name: block.name, arguments: JSON.stringify(block.arguments) },
			}));
		messages.push({
			role: "assistant",
			content: text || null,
			...(toolCalls.length ? { tool_calls: toolCalls } : {}),
		});
	}
	return messages;
}

function openAITools(context: Context): JsonRecord[] | undefined {
	if (!context.tools?.length) return undefined;
	return context.tools.map((tool) => ({
		type: "function",
		function: {
			name: tool.name,
			description: tool.description,
			parameters: tool.parameters,
		},
	}));
}

function stopReason(finishReason: string | null | undefined, hasTools: boolean): StopReason {
	if (finishReason === "length") return "length";
	if (hasTools) return "toolUse";
	return "stop";
}

function usage(envelope: ChatCompletionEnvelope) {
	const input = envelope.usage?.prompt_tokens ?? 0;
	const output = envelope.usage?.completion_tokens ?? 0;
	const cacheRead = envelope.usage?.prompt_tokens_details?.cached_tokens ?? 0;
	return {
		input,
		output,
		cacheRead,
		cacheWrite: 0,
		totalTokens: envelope.usage?.total_tokens ?? input + output,
		cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
	};
}

const PYTHON_TRANSPORT = String.raw`
import json
import os
import sys
import urllib.error
import urllib.request

payload = sys.stdin.buffer.read()
base_url = os.environ["PI_NONSTREAM_BASE_URL"].rstrip("/")
api_key = os.environ["PI_NONSTREAM_API_KEY"]
timeout = float(os.environ.get("PI_NONSTREAM_TIMEOUT_SECONDS", "600"))
request = urllib.request.Request(
    base_url + "/chat/completions",
    data=payload,
    headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
        status = response.status
        headers = dict(response.headers.items())
except urllib.error.HTTPError as error:
    body = error.read().decode("utf-8", errors="replace")
    status = error.code
    headers = dict(error.headers.items()) if error.headers else {}
print(json.dumps({"status": status, "headers": headers, "body": body}))
`;

interface TransportResponse {
	status: number;
	headers: Record<string, string>;
	body: string;
}

class OpenAICompatibleHTTPError extends Error {
	constructor(
		message: string,
		readonly status: number,
		readonly retryAfterMs: number,
	) {
		super(message);
	}
}

interface ModelEventLogPath {
	events?: string;
}

async function appendJsonLine(path: string | undefined, value: unknown): Promise<void> {
	if (!path) return;
	await appendFile(path, `${JSON.stringify(value)}\n`, { encoding: "utf8", mode: 0o600 });
}

function failedAssistantMessage(model: Model<any>, error: unknown, aborted = false): AssistantMessage {
	return {
		role: "assistant",
		content: [],
		api: model.api,
		provider: model.provider,
		model: model.id,
		usage: {
			input: 0,
			output: 0,
			cacheRead: 0,
			cacheWrite: 0,
			totalTokens: 0,
			cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
		},
		stopReason: aborted ? "aborted" : "error",
		errorMessage: errorMessage(error),
		timestamp: Date.now(),
	};
}

async function consumeAssistantStream(candidate: AssistantMessageEventStream): Promise<AssistantMessage> {
	for await (const event of candidate) {
		if (event.type === "done") return event.message;
		if (event.type === "error") return event.error;
	}
	return await candidate.result();
}

function emitAssistantMessage(stream: AssistantMessageEventStream, message: AssistantMessage): void {
	stream.push({ type: "start", partial: message });
	if (message.stopReason === "error" || message.stopReason === "aborted") {
		stream.push({ type: "error", reason: message.stopReason, error: message });
		return;
	}
	stream.push({ type: "done", reason: message.stopReason, message });
}

async function retryDelay(milliseconds: number, signal?: AbortSignal): Promise<void> {
	if (milliseconds <= 0 || signal?.aborted) return;
	await new Promise<void>((resolve) => {
		const timer = setTimeout(finish, milliseconds);
		function finish(): void {
			clearTimeout(timer);
			signal?.removeEventListener("abort", finish);
			resolve();
		}
		signal?.addEventListener("abort", finish, { once: true });
	});
}

/**
 * Retry a complete assistant request without exposing partial failed output to
 * the Agent.  Provider SDK retries cannot safely replay a stream that was
 * terminated after it emitted some chunks, which is the failure mode observed
 * from the OpenAI-compatible model gateway.  Buffering until the terminal
 * event keeps tool calls side-effect free and retries only the current request.
 */
export function retryingAssistantStream(
	model: Model<any>,
	factory: AssistantStreamFactory,
	options: RetryingStreamOptions,
): AssistantMessageEventStream {
	const stream = createAssistantMessageEventStream();
	const maxRetries = Math.max(0, Math.floor(options.maxRetries));
	const attempts = maxRetries + 1;
	const requestId = `stream_${++streamingRequestSequence}`;
	const label = options.label ?? "ontology-stage1";
	void (async () => {
		let lastMessage: AssistantMessage | undefined;
		for (let attempt = 1; attempt <= attempts; attempt++) {
			if (options.signal?.aborted) {
				lastMessage = failedAssistantMessage(model, "Request was aborted", true);
				break;
			}
			const startedAt = Date.now();
			await appendJsonLine(options.eventLogPath, {
				at: new Date(startedAt).toISOString(), type: "stream_request_start", requestId,
				label, attempt, attempts, provider: model.provider, model: model.id,
			});
			try {
				lastMessage = await consumeAssistantStream(await factory());
			} catch (error) {
				lastMessage = failedAssistantMessage(model, error, options.signal?.aborted === true);
			}
			const elapsedSeconds = (Date.now() - startedAt) / 1000;
			if (lastMessage.stopReason !== "error" && lastMessage.stopReason !== "aborted") {
				await appendJsonLine(options.eventLogPath, {
					at: new Date().toISOString(), type: "stream_request_complete", requestId,
					label, attempt, attempts, elapsedSeconds, stopReason: lastMessage.stopReason,
					usage: lastMessage.usage,
				});
				emitAssistantMessage(stream, lastMessage);
				return;
			}
			await appendJsonLine(options.eventLogPath, {
				at: new Date().toISOString(), type: "stream_request_error", requestId,
				label, attempt, attempts, elapsedSeconds, stopReason: lastMessage.stopReason,
				error: errorMessage(lastMessage.errorMessage ?? "unknown model request error"),
			});
			if (lastMessage.stopReason === "aborted" || attempt === attempts) break;
			const delayMs = Math.max(0, options.retryDelayMs ?? 5_000);
			await appendJsonLine(options.eventLogPath, {
				at: new Date().toISOString(), type: "stream_retry_wait", requestId,
				label, attempt, attempts, delaySeconds: delayMs / 1000,
			});
			await retryDelay(delayMs, options.signal);
		}
		emitAssistantMessage(
			stream,
			lastMessage ?? failedAssistantMessage(model, "Model request failed without a response"),
		);
	})();
	return stream;
}

function pythonRequest(
	baseUrl: string,
	apiKey: string,
	payload: unknown,
	options?: SimpleStreamOptions,
): Promise<TransportResponse> {
	return new Promise((resolve, reject) => {
		const python = process.env.DATAELF_PYTHON?.trim() || process.env.PYTHON?.trim() || "python3";
		const timeoutSeconds = Math.max(1, Math.ceil((options?.timeoutMs ?? 600_000) / 1000));
		const child = spawn(python, ["-c", PYTHON_TRANSPORT], {
			stdio: ["pipe", "pipe", "pipe"],
			env: {
				...process.env,
				PI_NONSTREAM_API_KEY: apiKey,
				PI_NONSTREAM_BASE_URL: baseUrl,
				PI_NONSTREAM_TIMEOUT_SECONDS: String(timeoutSeconds),
			},
		});
		let stdout = "";
		let stderr = "";
		const abort = () => child.kill("SIGTERM");
		options?.signal?.addEventListener("abort", abort, { once: true });
		child.stdout.setEncoding("utf8");
		child.stderr.setEncoding("utf8");
		child.stdout.on("data", (chunk: string) => {
			stdout += chunk;
			if (stdout.length > 20_000_000) child.kill("SIGTERM");
		});
		child.stderr.on("data", (chunk: string) => {
			if (stderr.length < 4000) stderr += chunk;
		});
		child.on("error", reject);
		child.on("close", (code) => {
			options?.signal?.removeEventListener("abort", abort);
			if (options?.signal?.aborted) return reject(new Error("Request was aborted"));
			if (code !== 0) return reject(new Error(`Python OpenAI transport exited ${code}: ${stderr.slice(-4000)}`));
			try {
				const result = JSON.parse(stdout) as TransportResponse;
				if (!Number.isInteger(result.status) || typeof result.body !== "string") throw new Error("invalid transport envelope");
				resolve(result);
			} catch (error) {
				reject(new Error(`Python OpenAI transport returned invalid JSON: ${errorMessage(error)}`));
			}
		});
		child.stdin.end(JSON.stringify(payload));
	});
}

function errorMessage(error: unknown): string {
	return (error instanceof Error ? error.message : String(error))
		.replace(/\b(Bearer|Basic)\s+[^\s,;]+/gi, "$1 [redacted]")
		.replace(/\bsk-[A-Za-z0-9_-]+\b/g, "[redacted-key]")
		.slice(0, 4000);
}

function retryAfterMilliseconds(response: TransportResponse): number {
	const header = Object.entries(response.headers).find(
		([name]) => name.toLowerCase() === "retry-after",
	)?.[1];
	const headerSeconds = Number(header);
	if (Number.isFinite(headerSeconds) && headerSeconds >= 0) {
		return Math.ceil(headerSeconds * 1000);
	}
	try {
		const body = JSON.parse(response.body) as {
			error?: { details?: { retry_after?: unknown } };
		};
		const bodySeconds = Number(body.error?.details?.retry_after);
		if (Number.isFinite(bodySeconds) && bodySeconds >= 0) {
			return Math.ceil(bodySeconds * 1000);
		}
	} catch {
		// The response body is retained in the public error below.
	}
	return 5000;
}

function shouldRetryRequest(error: unknown): boolean {
	if (error instanceof OpenAICompatibleHTTPError) {
		return error.status === 429 || error.status >= 500;
	}
	const message = errorMessage(error).toLowerCase();
	if (message.includes("timed out") || message.includes("timeout")) return false;
	return true;
}

function requestRetryDelayMilliseconds(error: unknown): number {
	if (error instanceof OpenAICompatibleHTTPError) {
		return Math.max(1000, error.retryAfterMs);
	}
	return 5000;
}

function configuredReasoningPayload(model: Model<any>, options?: SimpleStreamOptions): JsonRecord {
	if (!model.reasoning) return {};
	const compat = model.compat as Record<string, any> | undefined;
	const effort = options?.reasoning;
	if (compat?.thinkingFormat === "chat-template") {
		const kwargs: JsonRecord = {};
		for (const [key, value] of Object.entries(compat.chatTemplateKwargs ?? {})) {
			if (value && typeof value === "object") {
				const binding = value as { $var: string; omitWhenOff?: boolean };
				if (!effort && binding.omitWhenOff) continue;
				const resolved = binding.$var === "thinking.enabled" ? !!effort
					: effort ? model.thinkingLevelMap?.[effort] ?? effort : model.thinkingLevelMap?.off;
				if (resolved !== undefined && resolved !== null) kwargs[key] = resolved;
			} else kwargs[key] = value;
		}
		return { chat_template_kwargs: kwargs };
	}
	if (compat?.thinkingFormat === "zai" || compat?.thinkingFormat === "deepseek") {
		return { thinking: { type: effort ? "enabled" : "disabled" } };
	}
	if (compat?.supportsReasoningEffort && effort) return { reasoning_effort: model.thinkingLevelMap?.[effort] ?? effort };
	return {};
}

async function requestCompletion(
	model: Model<any>,
	context: Context,
	options: SimpleStreamOptions | undefined,
	logPaths?: ModelEventLogPath,
): Promise<{ envelope: ChatCompletionEnvelope; status: number; headers: Record<string, string>; offered: OfferedTools; required?: string }> {
	const apiKey = options?.apiKey?.trim();
	if (!apiKey) throw new Error(`No API key for provider: ${model.provider}`);
	let payload: unknown = {
		model: model.id,
		messages: openAIMessages(context),
		stream: false,
		parallel_tool_calls: false,
		...(options?.maxTokens !== undefined ? {max_tokens:options.maxTokens} : {}),
		...configuredReasoningPayload(model, options),
		...(openAITools(context) ? { tools: openAITools(context) } : {}),
		...((options as SimpleStreamOptions & { toolChoice?: unknown } | undefined)?.toolChoice !== undefined
			? { tool_choice: (options as SimpleStreamOptions & { toolChoice?: unknown }).toolChoice }
			: {}),
	};
	const transformed = await options?.onPayload?.(payload, model);
	if (transformed !== undefined) payload = transformed;
	if (payload && typeof payload === "object") {
		payload = { ...(payload as JsonRecord), stream: false };
	}

	const attempts = Math.max(1, (options?.maxRetries ?? 0) + 1);
	const payloadRecord = payload && typeof payload === "object" && !Array.isArray(payload)
		? payload as JsonRecord
		: {};
	const availableToolNames = Array.isArray(payloadRecord.tools)
		? payloadRecord.tools.map((value) => {
			if (!value || typeof value !== "object" || Array.isArray(value)) return "";
			return String((value as { function?: { name?: unknown } }).function?.name ?? "");
		}).filter(Boolean)
		: [];
	const requestedToolChoice = (() => {
		const value = payloadRecord.tool_choice;
		if (typeof value === "string") return value;
		if (!value || typeof value !== "object" || Array.isArray(value)) return null;
		return String((value as { function?: { name?: unknown } }).function?.name ?? "") || null;
	})();
	let lastError: unknown;
	for (let attempt = 1; attempt <= attempts; attempt++) {
        if (options?.signal?.aborted) throw new Error("Request was aborted before transport start");
		const startedAt = Date.now();
		await appendJsonLine(logPaths?.events, {
			at: new Date(startedAt).toISOString(), type: "request_start", attempt, attempts,
			provider: model.provider, model: model.id, timeoutMs: options?.timeoutMs ?? 600_000,
			availableToolNames, requestedToolChoice,
		});
		const heartbeat = setInterval(() => {
			void appendJsonLine(logPaths?.events, {
				at: new Date().toISOString(), type: "request_heartbeat", attempt,
				elapsedSeconds: Math.round((Date.now() - startedAt) / 1000),
			});
		}, 30_000);
		try {
			const response = await pythonRequest(model.baseUrl, apiKey, payload, options);
			await options?.onResponse?.({ status: response.status, headers: response.headers }, model);
			if (response.status < 200 || response.status >= 300) {
				throw new OpenAICompatibleHTTPError(
					`OpenAI-compatible endpoint returned HTTP ${response.status}: ${response.body.slice(0, 2000)}`,
					response.status,
					retryAfterMilliseconds(response),
				);
			}
			const envelope = JSON.parse(response.body) as ChatCompletionEnvelope;
			const firstMessage = envelope.choices?.[0]?.message;
			const compatibilityNames = process.env.DATAELF_SERVER_TOOL_PROTOCOL === "glm_text" && envelope.choices?.[0]?.finish_reason === "stop"
                ? parseTextualToolCalls(stripThinkingPrefix(firstMessage?.content ?? "")).toolCalls.map((c:any)=>c.function.name) : [];
            const returnedToolNames = (firstMessage?.tool_calls ?? [])
				.map((call) => call.function?.name?.trim())
				.filter((value): value is string => Boolean(value));
            if (!returnedToolNames.length) returnedToolNames.push(...compatibilityNames);
			await appendJsonLine(logPaths?.events, {
				at: new Date().toISOString(), type: "request_complete", attempt, status: response.status,
				elapsedSeconds: (Date.now() - startedAt) / 1000,
				usage: envelope.usage ?? null, finishReason: envelope.choices?.[0]?.finish_reason ?? null,
				// Keep diagnostics useful without recording prompts, tool arguments, or model text.
				toolCallNames: returnedToolNames,
                nativeToolCallNames:(firstMessage?.tool_calls ?? []).map(call=>call.function?.name),
                compatibleToolCallNames:compatibilityNames,
				toolCallArgumentKeys: (firstMessage?.tool_calls ?? []).map((call) => {
					try {
						const value = JSON.parse(call.function?.arguments ?? "{}") as unknown;
						return value && typeof value === "object" && !Array.isArray(value)
							? Object.keys(value as JsonRecord).sort()
							: [];
					} catch {
						return ["<invalid-json>"];
					}
				}),
				contentLength: typeof firstMessage?.content === "string" ? firstMessage.content.length : 0,
			});
            if (process.env.DATAELF_SERVER_TOOL_PROTOCOL!=='glm_text' && envelope.choices?.[0]?.finish_reason==='stop'
                && !(firstMessage?.tool_calls?.length) && /<tool_call>/.test(firstMessage?.content ?? '')) {
                throw new Error('PI_NATIVE_TOOL_CHANNEL_INCOMPATIBLE: Provider returned text tool markup without native tool calls (finish_reason=stop); no call was executed.');
            }
			if (
                !(process.env.DATAELF_SERVER_TOOL_PROTOCOL==='glm_text' && firstMessage?.content?.includes('<tool_call>')) &&
                envelope.choices?.[0]?.finish_reason !== 'length' &&
				requestedToolChoice
				&& availableToolNames.length === 1
				&& (
					returnedToolNames.length !== 1
					|| returnedToolNames[0] !== requestedToolChoice
				)
			) {
				throw new Error(
					`forced tool mismatch: requested ${requestedToolChoice}, received ${returnedToolNames.join(", ") || "none"}`,
				);
			}
			return { envelope, status: response.status, headers: response.headers,
                offered: new Map((Array.isArray(payloadRecord.tools) ? payloadRecord.tools : []).map((tool:any)=>[tool.function.name,tool.function.parameters])),
                required: typeof payloadRecord.tool_choice === "object" ? (payloadRecord.tool_choice as any)?.function?.name : undefined };
		} catch (error) {
			lastError = error;
			await appendJsonLine(logPaths?.events, {
				at: new Date().toISOString(), type: "request_error", attempt,
				elapsedSeconds: (Date.now() - startedAt) / 1000, error: errorMessage(error),
			});
			if (options?.signal?.aborted || attempt === attempts || !shouldRetryRequest(error)) break;
			const retryDelayMs = requestRetryDelayMilliseconds(error);
			await appendJsonLine(logPaths?.events, {
				at: new Date().toISOString(), type: "retry_wait", attempt,
				delaySeconds: retryDelayMs / 1000,
			});
			await new Promise((resolve) => setTimeout(resolve, retryDelayMs));
		} finally {
			clearInterval(heartbeat);
		}
	}
	throw lastError;
}

/**
 * Use an OpenAI-compatible non-streaming response and synthesize Pi's event stream.
 * This is for endpoints which complete `stream:false` requests but never emit SSE chunks.
 */
export function nonStreamingOpenAI(
	model: Model<any>,
	context: Context,
	options?: SimpleStreamOptions,
	logPaths?: ModelEventLogPath,
): AssistantMessageEventStream {
	const stream = createAssistantMessageEventStream();
	void (async () => {
		const output: AssistantMessage = {
			role: "assistant",
			content: [],
			api: model.api,
			provider: model.provider,
			model: model.id,
			usage: {
				input: 0,
				output: 0,
				cacheRead: 0,
				cacheWrite: 0,
				totalTokens: 0,
				cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
			},
			stopReason: "stop",
			timestamp: Date.now(),
		};
		try {
            if (options?.signal?.aborted) throw new Error("Request was aborted before transport start");
			const { envelope, offered, required } = await requestCompletion(model, context, options, logPaths);
			output.usage = usage(envelope);
            const choice = envelope.choices?.[0];
			const message = choice?.message;
			if (!message) throw new Error("OpenAI-compatible response has no first assistant message");
			// Strip thought markup before the legacy textual-tool parser can see it.
			if (message.content) message.content = stripThinkingPrefix(message.content);
            const finish=choice?.finish_reason;
            if (!finish || options?.signal?.aborted) throw new Error('Response has no confirmed terminal finish reason or was cancelled');
            if (!['stop','tool_calls','function_call','length'].includes(finish)) throw new Error(`Provider terminated with ${finish}`);
            const native=finish==='length' ? [] : message.tool_calls ?? [];
            const compatible=!native.length && finish==='stop' && process.env.DATAELF_SERVER_TOOL_PROTOCOL==='glm_text'
                ? decodeTextualTools(message.content ?? '',offered,finish,false,required) : [];
            // Validate every member before emitting an executable tool event.
            const toolCalls=native.map(call=>{
                const name=call.function?.name?.trim();
                if (!name) throw new Error('Native call has no function name');
                const args=JSON.parse(call.function?.arguments ?? '');
                validateNativeArguments(name,args,offered,required);
                return call;
            });
            toolCalls.push(...compatible.map(call=>({type:'function',function:{name:call.name,arguments:JSON.stringify(call.arguments)}})));
            const textualToolCalls={matched:compatible.length>0};
			output.responseId = envelope.id;
			if (envelope.model && envelope.model !== model.id) output.responseModel = envelope.model;
			output.usage = usage(envelope);
			stream.push({ type: "start", partial: output });

			if (message.content && !textualToolCalls.matched) {
				const block = { type: "text" as const, text: message.content };
				output.content.push(block);
				const contentIndex = output.content.length - 1;
				stream.push({ type: "text_start", contentIndex, partial: output });
				stream.push({ type: "text_delta", contentIndex, delta: block.text, partial: output });
				stream.push({ type: "text_end", contentIndex, content: block.text, partial: output });
			}
			for (const [index, call] of toolCalls.entries()) {
				if (call.type && call.type !== "function") continue;
				const name = call.function?.name?.trim();
				if (!name) throw new Error(`tool call ${index} has no function name`);
				const rawArguments = call.function?.arguments ?? "{}";
				let parsedArguments: unknown;
				try {
					parsedArguments = JSON.parse(rawArguments);
				} catch (error) {
					throw new Error(`tool call ${name} returned invalid JSON arguments: ${errorMessage(error)}`);
				}
				if (!parsedArguments || typeof parsedArguments !== "object" || Array.isArray(parsedArguments)) {
					throw new Error(`tool call ${name} arguments must be a JSON object`);
				}
				const block: ToolCall = {
					type: "toolCall",
					id: call.id || `call_${Date.now()}_${index}`,
					name,
					arguments: parsedArguments as JsonRecord,
				};
				output.content.push(block);
				const contentIndex = output.content.length - 1;
				stream.push({ type: "toolcall_start", contentIndex, partial: output });
				stream.push({ type: "toolcall_delta", contentIndex, delta: rawArguments, partial: output });
				stream.push({ type: "toolcall_end", contentIndex, toolCall: block, partial: output });
			}

			output.stopReason = stopReason(choice?.finish_reason, toolCalls.length > 0);
			stream.push({ type: "done", reason: output.stopReason as "stop" | "length" | "toolUse", message: output });
			stream.end(output);
		} catch (error) {
			output.stopReason = options?.signal?.aborted ? "aborted" : "error";
			output.errorMessage = errorMessage(error);
            if(error instanceof ToolEnvelopeError) (output as any).protocolFailure=error.diagnostic();
			stream.push({ type: "error", reason: output.stopReason, error: output });
			stream.end(output);
		}
	})();
	return stream;
}

export function immediateAssistantStop(model: Model<any>, text = "Completed."): AssistantMessageEventStream {
	const stream = createAssistantMessageEventStream();
	const message: AssistantMessage = {
		role: "assistant",
		content: [{ type: "text", text }],
		api: model.api,
		provider: model.provider,
		model: model.id,
		usage: {
			input: 0,
			output: 0,
			cacheRead: 0,
			cacheWrite: 0,
			totalTokens: 0,
			cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
		},
		stopReason: "stop",
		timestamp: Date.now(),
	};
	queueMicrotask(() => {
		stream.push({ type: "start", partial: { ...message, content: [] } });
		stream.push({ type: "text_start", contentIndex: 0, partial: message });
		stream.push({ type: "text_delta", contentIndex: 0, delta: text, partial: message });
		stream.push({ type: "text_end", contentIndex: 0, content: text, partial: message });
		stream.push({ type: "done", reason: "stop", message });
		stream.end(message);
	});
	return stream;
}
