#!/usr/bin/env node

import { spawn } from "node:child_process";
import { appendFileSync } from "node:fs";
import { readFile } from "node:fs/promises";
import {
	Agent,
	type AgentTool,
	type BeforeToolCallContext,
	type BeforeToolCallResult,
} from "../../../../../../../node_modules/@earendil-works/pi-coding-agent/node_modules/@earendil-works/pi-agent-core/dist/index.js";
import { type Api, type Model, streamSimple, Type } from "../../../../../../../node_modules/@earendil-works/pi-coding-agent/node_modules/@earendil-works/pi-ai/dist/compat.js";
import { VERSION } from "../../../../../../../node_modules/@earendil-works/pi-coding-agent/dist/index.js";
import { immediateAssistantStop, nonStreamingOpenAI, requiresNonStreamingOpenAI, retryingAssistantStream } from "./nonstream_openai.ts";

const API_KEY_ENV = "ONTOLOGY_STAGE1_REVIEWER_API_KEY";
type JsonRecord = Record<string, unknown>;

interface RuntimeConfig {
	provider: "openai" | "anthropic";
	model: string;
	baseUrl: string | null;
	contextWindow: number;
	maxTokens: number;
	temperature: number;
	requestTimeoutSeconds: number;
	requestMaxRetries: number;
	piVersion: string;
	systemPrompt: string;
	prompt: string;
	modelEventLogPath?: string;
	bridge: { pythonExecutable: string; path: string; evidencePath: string; maxOutputBytes: number };
}

function isRecord(value: unknown): value is JsonRecord {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

function message(error: unknown): string { return error instanceof Error ? error.message : String(error); }
function clean(value: unknown): string {
	return String(value)
		.replace(/\b(Bearer|Basic)\s+[^\s,;]+/gi, "$1 [redacted]")
		.replace(/\bsk-[A-Za-z0-9_-]+\b/g, "[redacted-key]")
		.replace(/(api[_ -]?key|authorization|token|secret)\s*[:=]\s*[^\s,;&]+/gi, "$1=[redacted]")
		.slice(0, 4000);
}
function json(value: unknown): string {
	const result = JSON.stringify(value);
	if (result === undefined) throw new Error("value is not JSON serializable");
	return result;
}

async function bridgeCall(config: RuntimeConfig, tool: string, args: JsonRecord, signal?: AbortSignal): Promise<unknown> {
	return await new Promise((resolve, reject) => {
		const child = spawn(config.bridge.pythonExecutable, [config.bridge.path, "--evidence", config.bridge.evidencePath, "--tool", tool], { stdio: ["pipe", "pipe", "pipe"] });
		let stdout = "";
		let stderr = "";
		let bytes = 0;
		const abort = () => child.kill("SIGTERM");
		signal?.addEventListener("abort", abort, { once: true });
		child.stdout.setEncoding("utf8");
		child.stderr.setEncoding("utf8");
		child.stdout.on("data", (chunk: string) => { bytes += Buffer.byteLength(chunk); if (bytes > config.bridge.maxOutputBytes) child.kill("SIGTERM"); else stdout += chunk; });
		child.stderr.on("data", (chunk: string) => { if (stderr.length < 4000) stderr += chunk; });
		child.on("error", reject);
		child.on("close", (code) => {
			signal?.removeEventListener("abort", abort);
			if (bytes > config.bridge.maxOutputBytes) return reject(new Error("evidence bridge output exceeded limit"));
			if (code !== 0) return reject(new Error(clean(stderr || `evidence bridge exited ${code}`)));
			try {
				const envelope: unknown = JSON.parse(stdout);
				if (!isRecord(envelope) || typeof envelope.isError !== "boolean") throw new Error("invalid evidence bridge envelope");
				if (envelope.isError) throw new Error(json(envelope.result));
				resolve(envelope.result);
			} catch (error) { reject(error); }
		});
		child.stdin.end(json(args));
	});
}

function toolResult(result: unknown) { return { content: [{ type: "text" as const, text: json(result) }], details: result }; }

function createTools(config: RuntimeConfig, onSubmit: (review: JsonRecord) => void): AgentTool[] {
	const execute = async (name: string, args: JsonRecord, signal?: AbortSignal) => toolResult(await bridgeCall(config, name, args, signal));
	const tools: AgentTool[] = [
		{
			name: "ontology_source_overview", label: "Source overview", description: "Read the controller-owned raw-backed normalized catalog and domain hints.",
			parameters: Type.Object({}, { additionalProperties: false }), executionMode: "sequential",
			execute: async (_id, _params, signal) => await execute("ontology_source_overview", {}, signal),
		},
		{
			name: "ontology_describe_table", label: "Describe table", description: "Read exact source schema and profiles for a table.",
			parameters: Type.Object({ table: Type.String({ minLength: 1 }) }, { additionalProperties: false }), executionMode: "sequential",
			execute: async (_id, params, signal) => await execute("ontology_describe_table", { table: params.table }, signal),
		},
		{
			name: "ontology_sample_rows", label: "Sample rows", description: "Read bounded source rows with replayable locators.",
			parameters: Type.Object({ table: Type.String({ minLength: 1 }) }, { additionalProperties: false }), executionMode: "sequential",
			execute: async (_id, params, signal) => await execute("ontology_sample_rows", { table: params.table }, signal),
		},
		{
			name: "ontology_profile_columns", label: "Profile columns", description: "Read null, distinct, lexical type, and JSON profiles.",
			parameters: Type.Object({ table: Type.String({ minLength: 1 }) }, { additionalProperties: false }), executionMode: "sequential",
			execute: async (_id, params, signal) => await execute("ontology_profile_columns", { table: params.table }, signal),
		},
		{
			name: "ontology_profile_identity", label: "Profile identity", description: "Verify identity uniqueness and duplicate semantics.",
			parameters: Type.Object({ table: Type.String(), columns: Type.Array(Type.String(), { minItems: 1, maxItems: 16 }) }, { additionalProperties: false }), executionMode: "sequential",
			execute: async (_id, params, signal) => await execute("ontology_profile_identity", { table: params.table, columns: params.columns }, signal),
		},
		{
			name: "ontology_validate_join", label: "Validate join", description: "Verify exact source join coverage and cardinality.",
			parameters: Type.Object({ source_table: Type.String(), source_columns: Type.Array(Type.String(), { minItems: 1, maxItems: 16 }), target_table: Type.String(), target_columns: Type.Array(Type.String(), { minItems: 1, maxItems: 16 }) }, { additionalProperties: false }), executionMode: "sequential",
			execute: async (_id, params, signal) => await execute("ontology_validate_join", params as JsonRecord, signal),
		},
		{
			name: "ontology_describe_raw_endpoint", label: "Raw endpoint", description: "Inspect raw responses and path profiles for an endpoint.",
			parameters: Type.Object({ endpoint: Type.String({ minLength: 1 }) }, { additionalProperties: false }), executionMode: "sequential",
			execute: async (_id, params, signal) => await execute("ontology_describe_raw_endpoint", { endpoint: params.endpoint }, signal),
		},
		{
			name: "ontology_profile_raw_path", label: "Raw path", description: "Inspect a raw JSON path pattern and classification.",
			parameters: Type.Object({ endpoint: Type.Optional(Type.String()), path_pattern: Type.Optional(Type.String()) }, { additionalProperties: false }), executionMode: "sequential",
			execute: async (_id, params, signal) => await execute("ontology_profile_raw_path", params as JsonRecord, signal),
		},
		{
			name: "ontology_trace_normalized_column", label: "Column lineage", description: "Inspect raw lineage, defaults, formulas, and ontology eligibility for table.column.",
			parameters: Type.Object({ coordinate: Type.String({ minLength: 1 }) }, { additionalProperties: false }), executionMode: "sequential",
			execute: async (_id, params, signal) => await execute("ontology_trace_normalized_column", { coordinate: params.coordinate }, signal),
		},
		{
			name: "ontology_compare_relation_sources", label: "Relation authority", description: "Compare authoritative and corroborating endpoints.",
			parameters: Type.Object({ relation: Type.Optional(Type.String()) }, { additionalProperties: false }), executionMode: "sequential",
			execute: async (_id, params, signal) => await execute("ontology_compare_relation_sources", params as JsonRecord, signal),
		},
		{
			name: "ontology_replay_source", label: "Source replay", description: "Inspect exhaustive raw document/record/fragment pointer replay status.",
			parameters: Type.Object({}, { additionalProperties: false }), executionMode: "sequential",
			execute: async (_id, _params, signal) => await execute("ontology_replay_source", {}, signal),
		},
	];
	const issue = Type.Object({
		severity: Type.Union([Type.Literal("critical"), Type.Literal("high"), Type.Literal("medium"), Type.Literal("low")]),
		category: Type.String({ minLength: 1 }), path: Type.String({ minLength: 1 }), message: Type.String({ minLength: 1 }),
		evidenceRefs: Type.Array(Type.String()), requiredChange: Type.String({ minLength: 1 }), acceptanceCriteria: Type.String({ minLength: 1 }),
	}, { additionalProperties: false });
	const check = Type.Object({
		status: Type.Union([Type.Literal("pass"), Type.Literal("fail")]),
		summary: Type.String({ minLength: 1 }),
		evidenceRefs: Type.Array(Type.String()),
	}, { additionalProperties: false });
	tools.push({
		name: "ontology_submit_review", label: "Submit review", description: "Submit the final structured independent review.",
		parameters: Type.Object({
			schemaVersion: Type.Literal("dataelf-ontology-review.v2"),
			verdict: Type.Union([Type.Literal("approve"), Type.Literal("revise"), Type.Literal("unusable")]),
			summary: Type.String({ minLength: 1 }), issues: Type.Array(issue), checkedEvidenceRefs: Type.Array(Type.String()),
			checks: Type.Object({
				informationCompleteness: check, sourceNavigability: check, missingnessSemantics: check,
				associationEndpoints: check, observationMetrics: check, multivalueConcepts: check,
				relationAuthority: check, competencyQuestionExecutability: check,
				instanceIdentity: check, constraintExecutability: check,
			}, { additionalProperties: false }),
		}, { additionalProperties: false }), executionMode: "sequential",
		execute: async (_id, params) => { onSubmit(params as JsonRecord); return toolResult({ accepted: true, verdict: params.verdict, issueCount: params.issues.length }); },
	});
	return tools;
}

function oneToolOnly(context: Pick<BeforeToolCallContext, "assistantMessage" | "toolCall">): BeforeToolCallResult | undefined {
	const calls = context.assistantMessage.content.filter((item) => item.type === "toolCall");
	if (calls.length <= 1 || calls[0]?.id === context.toolCall.id) return undefined;
	return { block: true, reason: "The independent reviewer must call tools sequentially." };
}

function exactToolChoice(name: string) {
	return { type: "function" as const, function: { name } };
}

function appendRuntimeEvent(path: string | undefined, event: JsonRecord): void {
	if (!path) return;
	try {
		appendFileSync(path, `${json({ at: new Date().toISOString(), ...event })}\n`, "utf8");
	} catch {
		// Diagnostic logging must never change ontology execution semantics.
	}
}

function buildConfiguredModel(config: RuntimeConfig): Model<Api> | undefined {
	if (!config.baseUrl) return undefined;
	const api: Api = config.provider === "openai" ? "openai-completions" : "anthropic-messages";
	return {
		id: config.model, name: config.model, provider: config.provider, api, baseUrl: config.baseUrl,
		reasoning: false, input: ["text"], cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
		contextWindow: config.contextWindow, maxTokens: config.maxTokens,
		...(config.provider === "openai" ? { compat: { supportsStore: false, supportsDeveloperRole: false, supportsReasoningEffort: false, supportsUsageInStreaming: true, maxTokensField: "max_tokens" as const, supportsStrictMode: false, supportsLongCacheRetention: false, thinkingFormat: "zai" as const } } : {}),
	} as Model<Api>;
}

function adaptOpenAI(payload: unknown): unknown {
	if (!isRecord(payload)) return payload;
	const result = { ...payload };
	delete result.temperature;
	result.parallel_tool_calls = false;
	result.thinking = { type: "disabled" };
	result.chat_template_kwargs = { enable_thinking: false };
	return result;
}

async function run(config: RuntimeConfig): Promise<JsonRecord> {
	if ((VERSION as string) !== config.piVersion) throw new Error(`Pi ${config.piVersion} required, found ${VERSION as string}`);
	const key = process.env[API_KEY_ENV]?.trim();
	if (!key) throw new Error(`${API_KEY_ENV} is required`);
	let submitted: JsonRecord | undefined;
	const model = buildConfiguredModel(config);
	if (!model) throw new Error(`unknown reviewer model ${config.provider}/${config.model}`);
	const agent = new Agent({
		initialState: { systemPrompt: config.systemPrompt, model, thinkingLevel: "off", tools: createTools(config, (review) => { submitted = review; }) },
		toolExecution: "sequential",
		beforeToolCall: (context) => {
			appendRuntimeEvent(config.modelEventLogPath, {
				type: "tool_call",
				toolName: context.toolCall.name,
			});
			return oneToolOnly(context);
		},
		streamFn: (requestModel, context, options) => {
			// ontology_submit_review is terminal for streaming and non-streaming
			// models alike.  Do not spend another request asking the model to stop:
			// unstable compatible endpoints may repeat the submit tool indefinitely.
			if (submitted) return immediateAssistantStop(requestModel, "Ontology review submitted.");
			const lastMessage = context.messages.at(-1);
			const deepSeekToolChoice = lastMessage?.role === "toolResult" && lastMessage.toolName === "ontology_submit_review" ? "none" : exactToolChoice("ontology_submit_review");
			const compactDeepSeek = requiresNonStreamingOpenAI(config.model);
			const requestOptions = { ...options, apiKey: key, temperature: config.temperature, maxTokens: config.maxTokens, timeoutMs: config.requestTimeoutSeconds * 1000, maxRetries: config.requestMaxRetries, reasoning: "off" as const, onPayload: config.provider === "openai" && !compactDeepSeek ? adaptOpenAI : options?.onPayload, ...(compactDeepSeek ? { toolChoice: deepSeekToolChoice } : {}) };
			return config.provider === "openai" && requiresNonStreamingOpenAI(config.model)
				? nonStreamingOpenAI(requestModel, context, requestOptions, { events: config.modelEventLogPath })
				: retryingAssistantStream(
					requestModel,
					() => streamSimple(requestModel, context, { ...requestOptions, maxRetries: 0 }),
					{
						maxRetries: config.requestMaxRetries,
						eventLogPath: config.modelEventLogPath,
						label: "ontology-reviewer",
						signal: requestOptions.signal,
					},
				);
		},
	});
	await agent.prompt(config.prompt);
	if (agent.state.errorMessage?.trim()) throw new Error(agent.state.errorMessage);
	if (!submitted) throw new Error("reviewer ended without ontology_submit_review");
	return submitted;
}

async function main(): Promise<void> {
	if (process.argv[2] !== "run" || !process.argv[3]) throw new Error("usage: reviewer_runtime.ts run <config.json>");
	const raw = await readFile(process.argv[3], "utf8");
	if (/apiKey|api_key|authorization/i.test(raw)) throw new Error("runtime config must not contain credentials");
	const config: unknown = JSON.parse(raw);
	if (!isRecord(config)) throw new Error("runtime config must be an object");
	const review = await run(config as unknown as RuntimeConfig);
	process.stdout.write(`${json({ protocolVersion: 1, ok: true, review })}\n`);
}

main().catch((error) => {
	process.stderr.write(`[ontology-reviewer] ${clean(message(error))}\n`);
	process.stdout.write(`${json({ protocolVersion: 1, ok: false, error: clean(message(error)) })}\n`);
	process.exitCode = 1;
});
