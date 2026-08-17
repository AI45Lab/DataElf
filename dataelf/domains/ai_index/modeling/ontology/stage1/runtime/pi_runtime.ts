#!/usr/bin/env node

import { spawn } from "node:child_process";
import { appendFileSync } from "node:fs";
import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import { dirname } from "node:path";
import {
	Agent,
	type AgentTool,
	type BeforeToolCallContext,
	type BeforeToolCallResult,
} from "../../../../../../../node_modules/@earendil-works/pi-coding-agent/node_modules/@earendil-works/pi-agent-core/dist/index.js";
import {
	type Api,
	type Model,
	streamSimple,
	Type,
} from "../../../../../../../node_modules/@earendil-works/pi-coding-agent/node_modules/@earendil-works/pi-ai/dist/compat.js";
import { VERSION } from "../../../../../../../node_modules/@earendil-works/pi-coding-agent/dist/index.js";
import { immediateAssistantStop, nonStreamingOpenAI, requiresNonStreamingOpenAI, retryingAssistantStream } from "./nonstream_openai.ts";

const API_KEY_ENV = "ONTOLOGY_STAGE1_API_KEY";
const REQUIRED_OBJECT_SECTIONS = [
	"metadata", "classes", "objectProperties", "datatypeProperties",
	"tableClassifications", "columnClassifications", "classEvidence",
	"objectPropertyEvidence", "datatypePropertyEvidence", "entityObservationMappings",
	"accessPaths", "domainHintResolutions", "cqCoverage", "sourceCoverage",
	"sourceBindings", "sourceAccessPaths", "rawPathClassifications", "associationMappings",
	"entityResolutionMappings", "responseObservationMappings", "relationAuthority",
] as const;
const REQUIRED_ARRAY_SECTIONS = ["competencyQuestions", "normalizationEvidenceRefs"] as const;
const DEEPSEEK_OBJECT_GROUPS = [
	["ontology_stage_core", ["metadata", "classes", "objectProperties", "datatypeProperties"]],
	["ontology_stage_table_domain", ["tableClassifications", "domainHintResolutions"]],
	["ontology_stage_columns", ["columnClassifications"]],
	["ontology_stage_raw_paths", ["rawPathClassifications"]],
	["ontology_stage_evidence", ["classEvidence", "objectPropertyEvidence", "datatypePropertyEvidence"]],
	["ontology_stage_source_bindings", ["sourceBindings"]],
	["ontology_stage_coverage", ["cqCoverage", "sourceCoverage", "sourceAccessPaths"]],
	["ontology_stage_mappings", ["entityObservationMappings", "accessPaths", "associationMappings", "entityResolutionMappings", "responseObservationMappings", "relationAuthority"]],
] as const;
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
	sourceFingerprint: string;
	stageStatePath: string;
	modelEventLogPath?: string;
	resume: boolean;
	seedSections?: Record<string, unknown> | null;
	bridge: { pythonExecutable: string; path: string; evidencePath: string; maxOutputBytes: number };
}

interface StageState {
	version: 2;
	sourceFingerprint: string;
	sections: Record<string, unknown>;
	completedSections: string[];
	updatedAt: string;
}

function isRecord(value: unknown): value is JsonRecord {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

function message(error: unknown): string {
	return error instanceof Error ? error.message : String(error);
}

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

async function atomicJson(path: string, value: unknown): Promise<void> {
	await mkdir(dirname(path), { recursive: true });
	const temporary = `${path}.${process.pid}.tmp`;
	await writeFile(temporary, `${JSON.stringify(value, null, 2)}\n`, { encoding: "utf8", mode: 0o600 });
	await rename(temporary, path);
}

async function loadStage(config: RuntimeConfig): Promise<StageState> {
	const seeded = isRecord(config.seedSections) ? config.seedSections : {};
	const seededCompleted = [...REQUIRED_OBJECT_SECTIONS, ...REQUIRED_ARRAY_SECTIONS]
		.filter((section) => section in seeded);
	if (config.resume) {
		try {
			const parsed: unknown = JSON.parse(await readFile(config.stageStatePath, "utf8"));
			if (isRecord(parsed) && parsed.version === 2 && parsed.sourceFingerprint === config.sourceFingerprint && isRecord(parsed.sections)) {
				const prior = parsed as unknown as StageState;
				return {
					...prior,
					sections: { ...seeded, ...prior.sections },
					completedSections: Array.from(new Set([...seededCompleted, ...prior.completedSections])).sort(),
				};
			}
		} catch (error) {
			if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
		}
	}
	return {
		version: 2,
		sourceFingerprint: config.sourceFingerprint,
		sections: { ...seeded },
		completedSections: [...seededCompleted].sort(),
		updatedAt: new Date().toISOString(),
	};
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
		child.stdout.on("data", (chunk: string) => {
			bytes += Buffer.byteLength(chunk);
			if (bytes > config.bridge.maxOutputBytes) child.kill("SIGTERM");
			else stdout += chunk;
		});
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

function toolResult(result: unknown) {
	return { content: [{ type: "text" as const, text: json(result) }], details: result };
}

function createTools(config: RuntimeConfig, state: StageState, onSubmit: (candidate: JsonRecord) => void): AgentTool[] {
	const execute = async (name: string, args: JsonRecord, signal?: AbortSignal) => toolResult(await bridgeCall(config, name, args, signal));
	const tools: AgentTool[] = [
		{
			name: "ontology_source_overview", label: "Source overview", description: "Read the raw-backed normalized source catalog and declared domain hints.",
			parameters: Type.Object({}, { additionalProperties: false }), executionMode: "sequential",
			execute: async (_id, _params, signal) => await execute("ontology_source_overview", {}, signal),
		},
		{
			name: "ontology_describe_table", label: "Describe table", description: "Read one normalized evidence-view schema and exact column profile.",
			parameters: Type.Object({ table: Type.String({ minLength: 1 }) }, { additionalProperties: false }), executionMode: "sequential",
			execute: async (_id, params, signal) => await execute("ontology_describe_table", { table: params.table }, signal),
		},
		{
			name: "ontology_sample_rows", label: "Sample rows", description: "Read bounded rows with replayable locators.",
			parameters: Type.Object({ table: Type.String({ minLength: 1 }) }, { additionalProperties: false }), executionMode: "sequential",
			execute: async (_id, params, signal) => await execute("ontology_sample_rows", { table: params.table }, signal),
		},
		{
			name: "ontology_profile_columns", label: "Profile columns", description: "Read exact null, distinct, lexical-type, and JSON profiles.",
			parameters: Type.Object({ table: Type.String({ minLength: 1 }) }, { additionalProperties: false }), executionMode: "sequential",
			execute: async (_id, params, signal) => await execute("ontology_profile_columns", { table: params.table }, signal),
		},
		{
			name: "ontology_profile_identity", label: "Profile identity", description: "Verify completeness, uniqueness, duplicates, and merge-key semantics.",
			parameters: Type.Object({ table: Type.String({ minLength: 1 }), columns: Type.Array(Type.String(), { minItems: 1, maxItems: 16 }) }, { additionalProperties: false }), executionMode: "sequential",
			execute: async (_id, params, signal) => await execute("ontology_profile_identity", { table: params.table, columns: params.columns }, signal),
		},
		{
			name: "ontology_validate_join", label: "Validate join", description: "Verify join coverage, uniqueness, and observed cardinality.",
			parameters: Type.Object({ source_table: Type.String(), source_columns: Type.Array(Type.String(), { minItems: 1, maxItems: 16 }), target_table: Type.String(), target_columns: Type.Array(Type.String(), { minItems: 1, maxItems: 16 }) }, { additionalProperties: false }), executionMode: "sequential",
			execute: async (_id, params, signal) => await execute("ontology_validate_join", params as JsonRecord, signal),
		},
		{
			name: "ontology_relationship_candidates", label: "Relationship hints", description: "Read declared relation hints and controller-owned join evidence references.",
			parameters: Type.Object({}, { additionalProperties: false }), executionMode: "sequential",
			execute: async (_id, _params, signal) => await execute("ontology_relationship_candidates", {}, signal),
		},
		{
			name: "ontology_describe_raw_endpoint", label: "Raw endpoint", description: "Inspect response documents and raw path profiles for one endpoint.",
			parameters: Type.Object({ endpoint: Type.String({ minLength: 1 }) }, { additionalProperties: false }), executionMode: "sequential",
			execute: async (_id, params, signal) => await execute("ontology_describe_raw_endpoint", { endpoint: params.endpoint }, signal),
		},
		{
			name: "ontology_profile_raw_path", label: "Raw path", description: "Inspect a raw JSON path pattern and its classification.",
			parameters: Type.Object({ endpoint: Type.Optional(Type.String()), path_pattern: Type.Optional(Type.String()) }, { additionalProperties: false }), executionMode: "sequential",
			execute: async (_id, params, signal) => await execute("ontology_profile_raw_path", params as JsonRecord, signal),
		},
		{
			name: "ontology_trace_normalized_column", label: "Column lineage", description: "Trace one table.column through normalization to exact raw paths and safety policy.",
			parameters: Type.Object({ coordinate: Type.String({ minLength: 1 }) }, { additionalProperties: false }), executionMode: "sequential",
			execute: async (_id, params, signal) => await execute("ontology_trace_normalized_column", { coordinate: params.coordinate }, signal),
		},
		{
			name: "ontology_compare_relation_sources", label: "Relation authority", description: "Compare authoritative and corroborating endpoints for a cross-endpoint relation.",
			parameters: Type.Object({ relation: Type.Optional(Type.String()) }, { additionalProperties: false }), executionMode: "sequential",
			execute: async (_id, params, signal) => await execute("ontology_compare_relation_sources", params as JsonRecord, signal),
		},
		{
			name: "ontology_replay_source", label: "Source replay", description: "Read the controller's exhaustive document/record/fragment pointer replay status.",
			parameters: Type.Object({}, { additionalProperties: false }), executionMode: "sequential",
			execute: async (_id, _params, signal) => await execute("ontology_replay_source", {}, signal),
		},
	];
	tools.push({
		name: "ontology_stage_object", label: "Stage object section", description: `Durably replace or merge one object section. Large maps must use <=40-key merge chunks and mark only the final chunk complete. Allowed: ${REQUIRED_OBJECT_SECTIONS.join(", ")}`,
		parameters: Type.Object({
			section: Type.String({ minLength: 1 }),
			value: Type.Object({}, { additionalProperties: true }),
			mode: Type.Optional(Type.Union([Type.Literal("replace"), Type.Literal("merge")])),
			complete: Type.Optional(Type.Boolean()),
		}, { additionalProperties: false }),
		executionMode: "sequential",
		execute: async (_id, params) => {
			if (!(REQUIRED_OBJECT_SECTIONS as readonly string[]).includes(params.section)) throw new Error(`unsupported object section: ${params.section}`);
			const mode = params.mode ?? "replace";
			const complete = params.complete ?? true;
			if (mode === "merge") {
				const prior = state.sections[params.section];
				state.sections[params.section] = { ...(isRecord(prior) ? prior : {}), ...params.value };
			} else {
				state.sections[params.section] = params.value;
			}
			if (complete) state.completedSections = Array.from(new Set([...state.completedSections, params.section])).sort();
			else state.completedSections = state.completedSections.filter((section) => section !== params.section);
			state.updatedAt = new Date().toISOString();
			await atomicJson(config.stageStatePath, state);
			return toolResult({ staged: params.section, mode, complete, stagedKeyCount: Object.keys(state.sections[params.section] as JsonRecord).length, completedSections: state.completedSections });
		},
	});
	tools.push({
		name: "ontology_stage_array", label: "Stage array section", description: "Durably stage an allowed array section.",
		parameters: Type.Object({ section: Type.String({ minLength: 1 }), value: Type.Array(Type.Any(), { minItems: 1 }) }, { additionalProperties: false }),
		executionMode: "sequential",
		execute: async (_id, params) => {
			if (!(REQUIRED_ARRAY_SECTIONS as readonly string[]).includes(params.section)) throw new Error(`unsupported array section: ${params.section}`);
			state.sections[params.section] = params.value;
			state.completedSections = Array.from(new Set([...state.completedSections, params.section])).sort();
			state.updatedAt = new Date().toISOString();
			await atomicJson(config.stageStatePath, state);
			return toolResult({ staged: params.section, completedSections: state.completedSections });
		},
	});
	for (const [toolName, sections] of DEEPSEEK_OBJECT_GROUPS) {
		const properties = Object.fromEntries(sections.map((section) => [section, Type.Object({}, { additionalProperties: true })]));
		tools.push({
			name: toolName,
			label: `Stage ${sections.join(", ")}`,
			description: `DeepSeek deterministic checkpoint group. Produce complete grounded values for exactly these sections: ${sections.join(", ")}.`,
			parameters: Type.Object(properties, { additionalProperties: false }),
			executionMode: "sequential",
			execute: async (_id, params) => {
				const values = params as unknown as JsonRecord;
				for (const section of sections) {
					const value = values[section];
					if (!isRecord(value)) throw new Error(`grouped object section ${section} requires an object value`);
					state.sections[section] = value;
					state.completedSections = Array.from(new Set([...state.completedSections, section])).sort();
				}
				state.updatedAt = new Date().toISOString();
				await atomicJson(config.stageStatePath, state);
				const required = [...REQUIRED_OBJECT_SECTIONS, ...REQUIRED_ARRAY_SECTIONS];
				return toolResult({ staged: sections, completedSections: state.completedSections, remainingSections: required.filter((section) => !state.completedSections.includes(section)) });
			},
		});
	}
	tools.push({
		name: "ontology_stage_arrays", label: "Stage required arrays", description: "DeepSeek deterministic checkpoint group. Produce complete competencyQuestions and normalizationEvidenceRefs arrays.",
		parameters: Type.Object({
			competencyQuestions: Type.Array(Type.Any(), { minItems: 1 }),
			normalizationEvidenceRefs: Type.Array(Type.String(), { minItems: 1 }),
		}, { additionalProperties: false }),
		executionMode: "sequential",
		execute: async (_id, params) => {
			state.sections.competencyQuestions = params.competencyQuestions;
			state.sections.normalizationEvidenceRefs = params.normalizationEvidenceRefs;
			state.completedSections = Array.from(new Set([...state.completedSections, ...REQUIRED_ARRAY_SECTIONS])).sort();
			state.updatedAt = new Date().toISOString();
			await atomicJson(config.stageStatePath, state);
			return toolResult({ staged: REQUIRED_ARRAY_SECTIONS, completedSections: state.completedSections, remainingSections: [] });
		},
	});
	tools.push({
		name: "ontology_stage_batch", label: "Stage section batch", description: `Durably apply up to eight section checkpoints in one call. Use merge chunks of <=40 keys for large object maps and complete=true only on the final chunk. Include as many remaining sections as fit. Allowed object sections: ${REQUIRED_OBJECT_SECTIONS.join(", ")}. Allowed array sections: ${REQUIRED_ARRAY_SECTIONS.join(", ")}.`,
		parameters: Type.Object({
			updates: Type.Array(Type.Object({
				section: Type.String({ minLength: 1 }),
				value: Type.Any(),
				mode: Type.Optional(Type.Union([Type.Literal("replace"), Type.Literal("merge")])),
				complete: Type.Optional(Type.Boolean()),
			}, { additionalProperties: false }), { minItems: 1, maxItems: 8 }),
		}, { additionalProperties: false }),
		executionMode: "sequential",
		execute: async (_id, params) => {
			const staged: JsonRecord[] = [];
			for (const update of params.updates) {
				const section = String(update.section);
				const isObjectSection = (REQUIRED_OBJECT_SECTIONS as readonly string[]).includes(section);
				const isArraySection = (REQUIRED_ARRAY_SECTIONS as readonly string[]).includes(section);
				if (!isObjectSection && !isArraySection) throw new Error(`unsupported batch section: ${section}`);
				const mode = update.mode ?? "replace";
				const complete = update.complete ?? true;
				if (isObjectSection) {
					if (!isRecord(update.value)) throw new Error(`object section ${section} requires an object value`);
					if (mode === "merge") {
						const prior = state.sections[section];
						state.sections[section] = { ...(isRecord(prior) ? prior : {}), ...update.value };
					} else {
						state.sections[section] = update.value;
					}
				} else {
					if (!Array.isArray(update.value) || update.value.length === 0) throw new Error(`array section ${section} requires a non-empty array value`);
					state.sections[section] = update.value;
				}
				if (complete) state.completedSections = Array.from(new Set([...state.completedSections, section])).sort();
				else state.completedSections = state.completedSections.filter((value) => value !== section);
				staged.push({ section, mode, complete });
			}
			state.updatedAt = new Date().toISOString();
			await atomicJson(config.stageStatePath, state);
			const required = [...REQUIRED_OBJECT_SECTIONS, ...REQUIRED_ARRAY_SECTIONS];
			return toolResult({ staged, completedSections: state.completedSections, remainingSections: required.filter((section) => !state.completedSections.includes(section)) });
		},
	});
	tools.push({
		name: "ontology_submit_candidate", label: "Submit candidate", description: "Assemble and submit all durably staged sections.",
		parameters: Type.Object({}, { additionalProperties: false }), executionMode: "sequential",
		execute: async () => {
			const required = [...REQUIRED_OBJECT_SECTIONS, ...REQUIRED_ARRAY_SECTIONS];
			const missing = required.filter((section) => !(section in state.sections));
			if (missing.length) throw new Error(`missing staged sections: ${missing.join(", ")}`);
			const ontology = {
				schemaVersion: "dataelf-ontology.v2", metadata: state.sections.metadata,
				classes: state.sections.classes, objectProperties: state.sections.objectProperties,
				datatypeProperties: state.sections.datatypeProperties,
			};
			const grounding: JsonRecord = { schemaVersion: "dataelf-grounding.v2", sourceFingerprint: config.sourceFingerprint };
			for (const section of required) if (!["metadata", "classes", "objectProperties", "datatypeProperties"].includes(section)) grounding[section] = state.sections[section];
			const candidate = { ontology, grounding };
			onSubmit(candidate);
			return toolResult({ accepted: true, ontologyClasses: Object.keys(state.sections.classes as JsonRecord).length, completedSections: state.completedSections });
		},
	});
	return tools;
}

function createSemanticPlanTools(config: RuntimeConfig, onSubmit: (candidate: JsonRecord) => void): AgentTool[] {
	return [{
		name: "ontology_submit_semantic_plan",
		label: "Submit semantic plan",
		description: "Submit the compact semantic decisions for the canonical raw-grounded AI Index ontology. The controller expands this into the exhaustive v2 grounding contract and validates every generated artifact.",
		parameters: Type.Object({
			description: Type.String({ minLength: 40, maxLength: 800 }),
			paperSemantics: Type.String({ minLength: 20, maxLength: 400 }),
			scholarSemantics: Type.String({ minLength: 20, maxLength: 400 }),
			institutionSemantics: Type.String({ minLength: 20, maxLength: 400 }),
			authorshipSemantics: Type.String({ minLength: 20, maxLength: 400 }),
			provenanceSemantics: Type.String({ minLength: 20, maxLength: 400 }),
			fundingEventPolicy: Type.Literal("omit_without_raw_instances"),
			stableValuePolicy: Type.Literal("require_consensus"),
			mutableMetricLayer: Type.Literal("observation"),
			relationshipArrayPolicy: Type.Literal("observation_snapshot_before_projection"),
		}, { additionalProperties: false }),
		executionMode: "sequential",
		execute: async (_id, params) => {
			const semanticPlan = params as unknown as JsonRecord;
			await atomicJson(config.stageStatePath, {
				version: 1,
				sourceFingerprint: config.sourceFingerprint,
				semanticPlan,
				updatedAt: new Date().toISOString(),
			});
			onSubmit({ schemaVersion: "dataelf-semantic-plan.v1", semanticPlan });
			return toolResult({ accepted: true, schemaVersion: "dataelf-semantic-plan.v1" });
		},
	}];
}

function oneToolOnly(context: Pick<BeforeToolCallContext, "assistantMessage" | "toolCall">): BeforeToolCallResult | undefined {
	const calls = context.assistantMessage.content.filter((item) => item.type === "toolCall");
	if (calls.length <= 1 || calls[0]?.id === context.toolCall.id) return undefined;
	return { block: true, reason: "Call ontology tools sequentially so checkpoints remain deterministic." };
}

function exactToolChoice(name: string) {
	return { type: "function" as const, function: { name } };
}

function appendRuntimeEvent(path: string, event: JsonRecord): void {
	try {
		appendFileSync(path, `${json({ at: new Date().toISOString(), ...event })}\n`, "utf8");
	} catch {
		// Diagnostic logging must never change ontology execution semantics.
	}
}

function generatorConvergenceToolChoice(
	state: StageState,
	initialCompletedSectionCount: number,
	resume: boolean,
	toolResultCount: number,
): { choice: unknown; reason: string } | undefined {
	const required = [...REQUIRED_OBJECT_SECTIONS, ...REQUIRED_ARRAY_SECTIONS];
	const incomplete = required.filter((section) => !state.completedSections.includes(section));
	// Let a fresh generator inspect evidence and choose its first semantic
	// checkpoint, but cap that orientation phase. Some GLM runs otherwise trace
	// normalized columns indefinitely without staging a single semantic section.
	// Once staging starts, a checkpoint is resumed, or eight evidence/tool calls
	// have completed, force one bounded checkpoint group per request.
	const semanticStagingStarted = resume || state.completedSections.length > initialCompletedSectionCount;
	const freshEvidenceBudgetExhausted = !resume && !semanticStagingStarted && toolResultCount >= 8;
	if (semanticStagingStarted || freshEvidenceBudgetExhausted) {
		const reasonPrefix = freshEvidenceBudgetExhausted ? "fresh_evidence_budget_exhausted" : "bounded_checkpoint_group";
		for (const [toolName, sections] of DEEPSEEK_OBJECT_GROUPS) {
			if (sections.some((section) => incomplete.includes(section))) {
				return { choice: exactToolChoice(toolName), reason: `${reasonPrefix}:${toolName}` };
			}
		}
		if (REQUIRED_ARRAY_SECTIONS.some((section) => incomplete.includes(section))) {
			return { choice: exactToolChoice("ontology_stage_arrays"), reason: `${reasonPrefix}:ontology_stage_arrays` };
		}
		if (!incomplete.length) {
			return { choice: exactToolChoice("ontology_submit_candidate"), reason: "all_sections_complete" };
		}
	}
	// sourceBindings is rebuilt deterministically by normalize_candidate_contract.
	// Once the model has staged any sourceBindings value and every other section
	// is complete, further evidence traversal cannot improve the executable
	// contract. Force the already-supported submit path instead of allowing an
	// unbounded sequence of read-only evidence calls.
	if (
		incomplete.length === 1
		&& incomplete[0] === "sourceBindings"
		&& isRecord(state.sections.sourceBindings)
	) return { choice: exactToolChoice("ontology_submit_candidate"), reason: "source_bindings_are_controller_rebuilt" };
	return undefined;
}

function generatorDeepSeekToolChoice(context: { messages: Array<{ role: string; toolName?: string }> }, state: StageState, repair: boolean): unknown {
	const calls = context.messages.filter((message) => message.role === "toolResult").map((message) => message.toolName);
	if (calls.at(-1) === "ontology_submit_candidate") return "none";
	if (repair) {
		const patchCalls = calls.filter((name) => name === "ontology_stage_batch").length;
		return patchCalls < 2 ? exactToolChoice("ontology_stage_batch") : exactToolChoice("ontology_submit_candidate");
	}
	for (const [toolName, sections] of DEEPSEEK_OBJECT_GROUPS) {
		if (sections.some((section) => !state.completedSections.includes(section))) return exactToolChoice(toolName);
	}
	if (REQUIRED_ARRAY_SECTIONS.some((section) => !state.completedSections.includes(section))) return exactToolChoice("ontology_stage_arrays");
	return exactToolChoice("ontology_submit_candidate");
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
	const state = await loadStage(config);
	const initialCompletedSectionCount = state.completedSections.length;
	let submitted: JsonRecord | undefined;
	const model = buildConfiguredModel(config);
	if (!model) throw new Error(`unknown model ${config.provider}/${config.model}`);
	const compactSemanticPlan = requiresNonStreamingOpenAI(config.model);
	const resumePacket = !compactSemanticPlan && state.completedSections.length ? `\n\nDurable staged state already exists. Preserve or replace it section-by-section:\n${json(state)}` : "";
	const deepSeekBatchInstruction = compactSemanticPlan
		? "\n\nDeepSeek transport contract: return exactly one ontology_submit_semantic_plan tool call. Keep the semantic descriptions concise and obey the four literal controller policies. Deterministic grounding, provenance, IRI and SHACL materialization happens after this call."
		: "";
	const agent = new Agent({
		initialState: {
			systemPrompt: config.systemPrompt + deepSeekBatchInstruction,
			model,
			thinkingLevel: "off",
			tools: compactSemanticPlan
				? createSemanticPlanTools(config, (candidate) => { submitted = candidate; })
				: createTools(config, state, (candidate) => { submitted = candidate; }),
		},
		toolExecution: "sequential",
		beforeToolCall: (context) => {
			appendRuntimeEvent(config.modelEventLogPath, {
				type: "tool_call",
				toolName: context.toolCall.name,
				completedSectionCount: state.completedSections.length,
			});
			return oneToolOnly(context);
		},
		streamFn: (requestModel, context, options) => {
			// A submit tool is the terminal action for every generator transport.
			// Asking the model for one more turn lets some OpenAI-compatible models
			// repeatedly call ontology_submit_candidate forever after a successful
			// submission.  End locally because the validated candidate is already in
			// controller memory and no further model output can change it.
			if (submitted) return immediateAssistantStop(requestModel, "Ontology candidate submitted.");
			const deepSeekToolChoice = exactToolChoice("ontology_submit_semantic_plan");
			const toolResultCount = context.messages.filter((message) => message.role === "toolResult").length;
			const convergence = !compactSemanticPlan
				? generatorConvergenceToolChoice(state, initialCompletedSectionCount, config.resume, toolResultCount)
				: undefined;
			if (convergence) appendRuntimeEvent(config.modelEventLogPath, {
				type: "convergence_forced",
				reason: convergence.reason,
				completedSectionCount: state.completedSections.length,
			});
			const requestOptions = { ...options, apiKey: key, temperature: config.temperature, maxTokens: config.maxTokens, timeoutMs: config.requestTimeoutSeconds * 1000, maxRetries: config.requestMaxRetries, reasoning: "off" as const, onPayload: config.provider === "openai" && !compactSemanticPlan ? adaptOpenAI : options?.onPayload, ...(compactSemanticPlan ? { toolChoice: deepSeekToolChoice } : convergence ? { toolChoice: convergence.choice } : {}) };
			return config.provider === "openai" && compactSemanticPlan
				? nonStreamingOpenAI(requestModel, context, requestOptions, { events: config.modelEventLogPath })
				: retryingAssistantStream(
					requestModel,
					() => streamSimple(requestModel, context, { ...requestOptions, maxRetries: 0 }),
					{
						maxRetries: config.requestMaxRetries,
						eventLogPath: config.modelEventLogPath,
						label: "ontology-generator",
						signal: requestOptions.signal,
					},
				);
		},
	});
	await agent.prompt(config.prompt + resumePacket);
	if (agent.state.errorMessage?.trim()) throw new Error(agent.state.errorMessage);
	if (!submitted) throw new Error("generator ended without ontology_submit_candidate");
	return submitted;
}

async function main(): Promise<void> {
	if (process.argv[2] !== "run" || !process.argv[3]) throw new Error("usage: pi_runtime.ts run <config.json>");
	const raw = await readFile(process.argv[3], "utf8");
	if (/apiKey|api_key|authorization/i.test(raw)) throw new Error("runtime config must not contain credentials");
	const config: unknown = JSON.parse(raw);
	if (!isRecord(config)) throw new Error("runtime config must be an object");
	const result = await run(config as unknown as RuntimeConfig);
	process.stdout.write(`${json({ protocolVersion: 1, ok: true, candidate: result })}\n`);
}

main().catch((error) => {
	process.stderr.write(`[ontology-generator] ${clean(message(error))}\n`);
	process.stdout.write(`${json({ protocolVersion: 1, ok: false, error: clean(message(error)) })}\n`);
	process.exitCode = 1;
});
