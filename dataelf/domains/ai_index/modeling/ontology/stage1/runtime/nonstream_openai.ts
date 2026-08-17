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
} from "../../../../../../../node_modules/@earendil-works/pi-coding-agent/node_modules/@earendil-works/pi-ai/dist/compat.js";

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
export function parseTextualToolCalls(content: string | null | undefined): TextualToolCallParseResult {
	let body = content?.trim() ?? "";
	if (!body.startsWith("<tool_call>")) return { matched: false, toolCalls: [] };
	while (body.startsWith("<tool_call>")) body = body.slice("<tool_call>".length).trimStart();
	while (body.endsWith("</tool_call>")) body = body.slice(0, -"</tool_call>".length).trimEnd();
	const nameMatch = /^([A-Za-z_][A-Za-z0-9_.:-]*)\s*/.exec(body);
	if (!nameMatch) return { matched: false, toolCalls: [] };
	const name = nameMatch[1];
	const argumentsObject: JsonRecord = {};
	const pairPattern = /<arg_key>([\s\S]*?)<\/arg_key>\s*<arg_value>([\s\S]*?)<\/arg_value>/g;
	let cursor = nameMatch[0].length;
	let pairCount = 0;
	for (let match = pairPattern.exec(body); match; match = pairPattern.exec(body)) {
		if (body.slice(cursor, match.index).trim()) return { matched: false, toolCalls: [] };
		const key = match[1].trim();
		if (!key || Object.hasOwn(argumentsObject, key)) return { matched: false, toolCalls: [] };
		argumentsObject[key] = match[2];
		cursor = pairPattern.lastIndex;
		pairCount += 1;
	}
	if (!pairCount || body.slice(cursor).trim()) return { matched: false, toolCalls: [] };
	return {
		matched: true,
		toolCalls: [{
			id: `call_text_${Date.now()}`,
			type: "function",
			function: { name, arguments: JSON.stringify(argumentsObject) },
		}],
	};
}

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
	if (hasTools) return "toolUse";
	if (finishReason === "length") return "length";
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

async function requestCompletion(
	model: Model<any>,
	context: Context,
	options: SimpleStreamOptions | undefined,
	logPaths?: ModelEventLogPath,
): Promise<{ envelope: ChatCompletionEnvelope; status: number; headers: Record<string, string> }> {
	const apiKey = options?.apiKey?.trim();
	if (!apiKey) throw new Error(`No API key for provider: ${model.provider}`);
	let payload: unknown = {
		model: model.id,
		messages: openAIMessages(context),
		stream: false,
		parallel_tool_calls: false,
		...(options?.maxTokens ? { max_tokens: options.maxTokens } : {}),
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
	let lastError: unknown;
	for (let attempt = 1; attempt <= attempts; attempt++) {
		const startedAt = Date.now();
		await appendJsonLine(logPaths?.events, {
			at: new Date(startedAt).toISOString(), type: "request_start", attempt, attempts,
			provider: model.provider, model: model.id, timeoutMs: options?.timeoutMs ?? 600_000,
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
				throw new Error(`OpenAI-compatible endpoint returned HTTP ${response.status}: ${response.body.slice(0, 2000)}`);
			}
			const envelope = JSON.parse(response.body) as ChatCompletionEnvelope;
			await appendJsonLine(logPaths?.events, {
				at: new Date().toISOString(), type: "request_complete", attempt, status: response.status,
				elapsedSeconds: (Date.now() - startedAt) / 1000,
				usage: envelope.usage ?? null, finishReason: envelope.choices?.[0]?.finish_reason ?? null,
			});
			return { envelope, status: response.status, headers: response.headers };
		} catch (error) {
			lastError = error;
			await appendJsonLine(logPaths?.events, {
				at: new Date().toISOString(), type: "request_error", attempt,
				elapsedSeconds: (Date.now() - startedAt) / 1000, error: errorMessage(error),
			});
			if (options?.signal?.aborted || attempt === attempts) break;
			await appendJsonLine(logPaths?.events, {
				at: new Date().toISOString(), type: "retry_wait", attempt, delaySeconds: 5,
			});
			await new Promise((resolve) => setTimeout(resolve, 5_000));
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
			const { envelope } = await requestCompletion(model, context, options, logPaths);
			const choice = envelope.choices?.[0];
			const message = choice?.message;
			if (!message) throw new Error("OpenAI-compatible response has no first assistant message");
			const textualToolCalls = message.tool_calls?.length
				? { matched: false, toolCalls: [] }
				: parseTextualToolCalls(message.content);
			const toolCalls = message.tool_calls?.length ? message.tool_calls : textualToolCalls.toolCalls;
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

export function requiresNonStreamingOpenAI(modelName: string): boolean {
	const configured = (process.env.PI_NON_STREAMING_MODELS ?? "deepseek-v4-pro")
		.split(",")
		.map((value) => value.trim())
		.filter(Boolean);
	return configured.includes(modelName);
}
