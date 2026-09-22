// Finance tools behind the `sql` and `python` flags: a read-only SQLite trio
// plus a workspace-bound Python trio, bridged to tools.py. Tool names
// intentionally match the DDR/MedAgentGym/EHRFlowBench benchmark family so
// training trajectories transfer to those evaluation harnesses. Web tools
// are provided by the common pi-web-access package and are not registered
// here. The sibling finance_edgar_prices.ts registers the `edgar`/`prices`
// group.
//
// Which tools are registered is gated by DATAELF_FINANCE_ALLOWED_TOOLS
// (set by the domain plugin from the effective tool flags).

import { spawn } from "node:child_process";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "@earendil-works/pi-ai";

function callPython(tool: string, args: Record<string, unknown>, signal?: AbortSignal) {
	return new Promise<{ content: Array<{ type: "text"; text: string }>; details: unknown }>((resolve, reject) => {
		const child = spawn(process.env.DATAELF_PYTHON || "python3", ["-m", "dataelf.domains.finance.tools"], {
			cwd: process.env.DATAELF_WORKSPACE,
			env: process.env,
			stdio: ["pipe", "pipe", "pipe"],
		});
		let stdout = "";
		let stderr = "";
		child.stdout.on("data", (chunk) => { stdout += String(chunk); });
		child.stderr.on("data", (chunk) => { stderr += String(chunk); });
		const abort = () => child.kill("SIGTERM");
		if (signal) signal.addEventListener("abort", abort, { once: true });
		child.on("error", reject);
		child.on("close", (code) => {
			if (signal) signal.removeEventListener("abort", abort);
			if (code !== 0) return reject(new Error(stderr || `finance tool exited with ${code}`));
			try {
				const result = JSON.parse(stdout || "{}");
				if (result.error) return reject(new Error(result.error.message || JSON.stringify(result.error)));
				resolve({ content: [{ type: "text", text: JSON.stringify(result) }], details: result });
			} catch (error) { reject(new Error(`invalid finance tool response: ${String(error)}`)); }
		});
		child.stdin.end(JSON.stringify({ tool, arguments: args }));
	});
}

export default function financeSqlPython(pi: ExtensionAPI) {
	const configured = (process.env.DATAELF_FINANCE_ALLOWED_TOOLS || "").split(",").filter(Boolean);
	const allowed = new Set(configured);
	// Tool call budget: past DATAELF_FINANCE_MAX_TOOL_CALLS the finance tools
	// refuse with an instruction to finalize. Both finance extension files
	// count against one shared budget via globalThis (same Pi process).
	const budget = Number(process.env.DATAELF_FINANCE_MAX_TOOL_CALLS || "0") | 0;
	const state = ((globalThis as any).__dataelfFinanceToolBudget ||= { used: 0 }) as { used: number };
	const budgeted = (tool: any) => ({
		...tool,
		execute: async (id: any, params: any, signal: AbortSignal | undefined, ...rest: unknown[]) => {
			if (budget > 0 && state.used >= budget) {
				throw new Error(`Tool call budget exhausted (${budget} calls). Stop calling tools and write the declared output artifacts now.`);
			}
			state.used += 1;
			return tool.execute(id, params, signal, ...rest);
		},
	});
	const register = (tool: any) => {
		if (allowed.size === 0 || allowed.has(tool.name)) pi.registerTool(budgeted(tool));
	};
	register({
		name: "get_database_info", label: "SQLite database info",
		description: "Get general information about the authorized local finance SQLite database.",
		parameters: Type.Object({}, { additionalProperties: false }), executionMode: "sequential",
		execute: async (_id, _params, signal) => callPython("get_database_info", {}, signal),
	});
	register({
		name: "describe_table", label: "Describe SQLite table",
		description: "Get columns and schema details for an authorized finance table.",
		parameters: Type.Object({ table_name: Type.String({ minLength: 1 }) }, { additionalProperties: false }), executionMode: "sequential",
		execute: async (_id, params, signal) => callPython("describe_table", params, signal),
	});
	register({
		name: "execute_query", label: "Execute SQLite query",
		description: "Execute a read-only SQL query against the authorized finance database. Prefer explicit column selection; default 20 rows, hard cap 100, oversized cell values are clipped and reported in truncated_cells.",
		parameters: Type.Object({ query: Type.String({ minLength: 1 }), limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 100, description: "Maximum rows returned; default 20, hard cap 100" })) }, { additionalProperties: false }), executionMode: "sequential",
		execute: async (_id, params, signal) => callPython("execute_query", params, signal),
	});
	register({
		name: "execute_code", label: "Execute analysis code",
		description: "Execute read-only Python analysis code in the authorized finance workspace. Print summaries, statistics, and samples instead of full datasets; stdout is capped at 20000 characters and stderr at 12000 (stdout_truncated/stderr_truncated report clipping).",
		parameters: Type.Object({ code: Type.String({ minLength: 1 }), timeout: Type.Optional(Type.Integer({ minimum: 1, maximum: 300 })) }, { additionalProperties: false }), executionMode: "sequential",
		execute: async (_id, params, signal) => callPython("execute_code", params, signal),
	});
	register({
		name: "list_files", label: "List finance files",
		description: "List files in the authorized finance workspace. Prefer a path, a pattern, and recursive=false to keep listings small; default 100 items, hard cap 500.",
		parameters: Type.Object({ path: Type.Optional(Type.String()), pattern: Type.Optional(Type.String()), recursive: Type.Optional(Type.Boolean()), limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 500, description: "Maximum files returned; default 100, hard cap 500" })) }, { additionalProperties: false }), executionMode: "sequential",
		execute: async (_id, params, signal) => callPython("list_files", params, signal),
	});
	register({
		name: "get_field_description", label: "Get field description",
		description: "Get fields for a finance data file.",
		parameters: Type.Object({ data_file: Type.String({ minLength: 1 }) }, { additionalProperties: false }), executionMode: "sequential",
		execute: async (_id, params, signal) => callPython("get_field_description", params, signal),
	});
}
