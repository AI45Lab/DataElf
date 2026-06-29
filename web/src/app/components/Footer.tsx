import { useState, useEffect, useRef } from 'react';
import { cn } from '../utils';
import { buildRunSummaryRows } from '../api/runDisplay.js';

interface Attempt {
  id: string;
  action_type: string;
  score: number;
  status: 'success' | 'failed' | 'running';
  validation: 'passed' | 'failed' | 'pending';
  produced_candidate: boolean;
  result?: {
    goal_satisfied: boolean;
    score: number;
    failure_type: string;
    recommended_next_action: string;
    capability_gap: any;
    reason: string;
  };
}

interface RunResultData {
  rawResult?: any;
  allFailed?: boolean;
  error?: string | null;
  jobId?: string | null;
}

interface FooterProps {
  pipelineTools?: string[];
  logs?: string[];
  attempts?: Attempt[];
  candidateJson?: string | null;
  runResultData?: RunResultData | null;
  height?: number;
}

export function Footer({ pipelineTools = [], logs = [], attempts = [], candidateJson = null, runResultData = null, height = 200 }: FooterProps) {
  const [activeTab, setActiveTab] = useState('Logs');
  const logsEndRef = useRef<HTMLDivElement>(null);
  const runSummaryRows = buildRunSummaryRows(runResultData?.rawResult);

  // Auto-scroll logs when new logs are added
  useEffect(() => {
    if (logsEndRef.current && activeTab === 'Logs') {
      logsEndRef.current.scrollIntoView({ behavior: 'smooth' });
    }
  }, [logs, activeTab]);

  const tabs = ['Logs', 'Attempts', 'Candidate JSON', 'Result'];

  return (
    <footer className="border-t border-gray-700/60 bg-black/80 flex flex-col shrink-0 relative z-20">
      <div className="flex w-full overflow-x-auto scrollbar-hide bg-black/40">
        {tabs.map(tab => (
          <button
            key={tab}
            onClick={() => setActiveTab(tab)}
            className={cn(
              "px-6 py-2 text-sm font-medium uppercase tracking-wider transition-colors border-r border-gray-700/40 relative whitespace-nowrap",
              activeTab === tab
                ? "text-black bg-white hover:bg-gray-200 font-bold"
                : "text-gray-400 hover:bg-gray-900/20 hover:text-white"
            )}
          >
            {activeTab === tab && (
              <span className="absolute left-2 top-1/2 -translate-y-1/2 text-black">
                {'> '}
              </span>
            )}
            <span className={activeTab === tab ? "pl-4" : ""}>{tab}</span>
          </button>
        ))}
        <div className="flex-1 px-4 flex items-center justify-end text-xs text-gray-600">
          DataElf Core v1.2.0-rc
        </div>
      </div>

      {activeTab === 'Logs' && logs.length > 0 && (
        <div
          className="p-4 border-t border-gray-700/30 bg-black font-mono animate-in slide-in-from-bottom-2 overflow-y-auto custom-scrollbar flex flex-col gap-0.5"
          style={{ height: `${height - 50}px` }}
        >
          {logs.map((log, idx) => {
            // Guard against undefined/null logs
            if (!log && log !== '') return null;

            const logStr = String(log);

            // Determine color based on content
            const isError = logStr.includes('❌') || logStr.includes('[ERROR]') || logStr.includes('failed');
            const isSuccess = logStr.includes('✅') || logStr.includes('[SUCCESS]') || logStr.includes('[DONE]');
            const isWarning = logStr.includes('❓');
            const isComplete = logStr.includes('🏆');
            const isSeparator = logStr.includes('==========');
            const isMetric = logStr.includes('Judge Score') || logStr.includes('Metrics') || logStr.includes('Next Action');

            return (
              <div key={idx} className={cn(
                "text-xs leading-relaxed",
                isSeparator ? 'text-cyan-500 font-bold mt-2' :
                isError ? 'text-red-400' :
                isSuccess ? 'text-green-400' :
                isWarning ? 'text-yellow-400' :
                isComplete ? 'text-white font-bold' :
                isMetric ? 'text-white' :
                'text-gray-400'
              )}>
                {logStr}
              </div>
            );
          })}
          <div ref={logsEndRef} />
        </div>
      )}

      {activeTab === 'Logs' && logs.length === 0 && (
        <div className="p-4 border-t border-gray-700/30 bg-black font-mono opacity-50 flex items-center gap-2 text-gray-500 text-sm">
          <span>{'>'}</span> No logs generated yet.
        </div>
      )}

      {activeTab === 'Attempts' && attempts.length > 0 && (
        <div
          className="p-4 border-t border-gray-700/30 bg-black font-mono animate-in slide-in-from-bottom-2 overflow-y-auto custom-scrollbar"
          style={{ height: `${height - 50}px` }}
        >
          <table className="w-full text-left text-xs text-gray-400 border-collapse">
            <thead>
              <tr className="border-b border-gray-700/40 text-gray-500 uppercase tracking-widest text-[10px]">
                <th className="pb-2 font-normal">Attempt ID</th>
                <th className="pb-2 font-normal">Action Type</th>
                <th className="pb-2 font-normal">Score</th>
                <th className="pb-2 font-normal">Validation</th>
                <th className="pb-2 font-normal">Status</th>
                <th className="pb-2 font-normal">Candidate</th>
              </tr>
            </thead>
            <tbody>
              {attempts.map((att, idx) => (
                <tr key={idx} className="border-b border-gray-700/20 last:border-0 hover:bg-gray-900/20">
                  <td className="py-2 text-white font-bold">{att.id}</td>
                  <td className="py-2 text-cyan-400">{att.action_type}</td>
                  <td className={`py-2 font-bold ${att.score > 80 ? 'text-green-400' : 'text-yellow-400'}`}>{att.score.toFixed(1)}</td>
                  <td className={`py-2 ${att.validation === 'passed' ? 'text-green-400' : 'text-red-400'}`}>{att.validation.toUpperCase()}</td>
                  <td className={`py-2 ${att.status === 'success' ? 'text-green-400' : 'text-red-400'}`}>{att.status.toUpperCase()}</td>
                  <td className="py-2">{att.produced_candidate ? 'YES' : '-'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {activeTab === 'Attempts' && attempts.length === 0 && (
        <div className="p-4 border-t border-gray-700/30 bg-black font-mono opacity-50 flex items-center gap-2 text-gray-500 text-sm">
          <span>{'>'}</span> No attempts recorded. Only available in PILOT mode.
        </div>
      )}

      {activeTab === 'Candidate JSON' && candidateJson && (
        <div
          className="p-4 border-t border-gray-700/30 bg-black font-mono animate-in slide-in-from-bottom-2 overflow-y-auto custom-scrollbar"
          style={{ height: `${height - 50}px` }}
        >
          <pre className="text-xs text-gray-300 whitespace-pre-wrap">
            {candidateJson}
          </pre>
        </div>
      )}

      {activeTab === 'Candidate JSON' && !candidateJson && (
        <div className="p-4 border-t border-gray-700/30 bg-black font-mono opacity-50 flex items-center gap-2 text-gray-500 text-sm">
          <span>{'>'}</span> No candidate JSON generated yet.
        </div>
      )}

      {activeTab === 'Result' && runResultData && (
        <div
          className="p-4 border-t border-gray-700/30 bg-black font-mono animate-in slide-in-from-bottom-2 overflow-y-auto custom-scrollbar"
          style={{ height: `${height - 50}px` }}
        >
          <div className={cn(
            "border p-3 rounded bg-black/40",
            runResultData.allFailed ? "border-red-600/40" : "border-gray-700/50"
          )}>
            <div className={cn(
              "text-xs font-bold uppercase tracking-wider mb-3 pb-2 border-b flex items-center justify-between gap-4",
              runResultData.allFailed ? "text-red-400 border-red-600/30" : "text-white border-gray-700/40"
            )}>
              <span>Run Summary Result</span>
              {runResultData.jobId && (
                <span className="text-[10px] text-gray-500 normal-case tracking-normal">{runResultData.jobId}</span>
              )}
            </div>

            {runResultData.error && (
              <div className="mb-3 p-2 border border-red-900/50 bg-red-950/20 text-red-300 text-xs whitespace-pre-wrap">
                {runResultData.error}
              </div>
            )}

            {runSummaryRows.length > 0 ? (
              <div className="grid grid-cols-1 gap-2 text-xs">
                {runSummaryRows.map(row => (
                  <div key={row.key} className="grid grid-cols-[190px_1fr] gap-4 border-b border-gray-800/70 pb-2 last:border-0 last:pb-0">
                    <span className="text-gray-500">{row.key}</span>
                    {row.isNested ? (
                      <pre className="text-gray-300 whitespace-pre-wrap leading-relaxed overflow-x-auto">
                        {row.value}
                      </pre>
                    ) : (
                      <span className="text-gray-200">{row.value}</span>
                    )}
                  </div>
                ))}
              </div>
            ) : !runResultData.error ? (
              <div className="text-xs text-gray-500">No summary result returned.</div>
            ) : null}
          </div>
        </div>
      )}

      {activeTab === 'Result' && !runResultData && attempts.length > 0 && (
        <div
          className="p-4 border-t border-gray-700/30 bg-black font-mono animate-in slide-in-from-bottom-2 overflow-y-auto custom-scrollbar"
          style={{ height: `${height - 50}px` }}
        >
          <div className="flex flex-col gap-6">
            {attempts.map((att, idx) => {
              if (!att.result) return null;

              const res = att.result;
              const isSuccess = res.goal_satisfied;

              return (
                <div key={idx} className={cn(
                  "border p-3 rounded",
                  isSuccess ? "border-green-600/40 bg-green-950/20" : "border-yellow-600/40 bg-yellow-950/20"
                )}>
                  <div className={cn(
                    "text-xs font-bold uppercase tracking-wider mb-2 pb-2 border-b",
                    isSuccess ? "text-green-400 border-green-600/30" : "text-yellow-400 border-yellow-600/30"
                  )}>
                    {att.id} Result {isSuccess ? '✅' : '❌'}
                  </div>

                  <div className="grid grid-cols-2 gap-x-6 gap-y-2 text-xs mb-3">
                    <div>
                      <span className="text-gray-500">Goal Satisfied:</span>
                      <span className={cn(
                        "ml-2 font-bold",
                        res.goal_satisfied ? "text-green-400" : "text-red-400"
                      )}>
                        {res.goal_satisfied ? 'TRUE' : 'FALSE'}
                      </span>
                    </div>
                    <div>
                      <span className="text-gray-500">Score:</span>
                      <span className={cn(
                        "ml-2 font-bold",
                        res.score >= 0.8 ? "text-green-400" : res.score >= 0.5 ? "text-yellow-400" : "text-red-400"
                      )}>
                        {res.score.toFixed(2)}
                      </span>
                    </div>
                    <div>
                      <span className="text-gray-500">Failure Type:</span>
                      <span className={cn(
                        "ml-2",
                        res.failure_type === 'none' ? "text-green-400" : "text-red-400"
                      )}>
                        {res.failure_type}
                      </span>
                    </div>
                    <div>
                      <span className="text-gray-500">Next Action:</span>
                      <span className="ml-2 text-cyan-400">{res.recommended_next_action}</span>
                    </div>
                  </div>

                  {res.capability_gap && Object.keys(res.capability_gap).length > 0 && (
                    <div className="mb-3 p-2 bg-black/40 border border-gray-700/30 rounded">
                      <div className="text-[10px] uppercase tracking-wider text-gray-500 mb-1">Capability Gap</div>
                      <pre className="text-[10px] text-gray-300 whitespace-pre-wrap">
                        {JSON.stringify(res.capability_gap, null, 2)}
                      </pre>
                    </div>
                  )}

                  <div className="p-2 bg-black/40 border border-gray-700/30 rounded">
                    <div className="text-[10px] uppercase tracking-wider text-gray-500 mb-1">Reason</div>
                    <div className="text-xs text-gray-300 leading-relaxed">
                      {res.reason}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {activeTab === 'Result' && !runResultData && attempts.length === 0 && (
        <div className="p-4 border-t border-gray-700/30 bg-black font-mono opacity-50 flex items-center gap-2 text-gray-500 text-sm">
          <span>{'>'}</span> No result available yet.
        </div>
      )}
    </footer>
  );
}
