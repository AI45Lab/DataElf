import { useState } from 'react';
import { cn } from '../utils';

export function RightSidebar({ 
  activeTool, 
  candidateTools,
  onCandidateToolClick
}: { 
  activeTool?: string | null, 
  candidateTools?: { id: string, name: string, status: 'pending' | 'stable', sessionId?: string, messageId?: string }[] | null,
  onCandidateToolClick?: (sessionId: string, messageId: string) => void
}) {
  const [expandedNodes, setExpandedNodes] = useState<Set<string>>(
    new Set(['agent_tools', 'agent_tools_1', 'agent_tools_2', 'agent_tools_3', 'custom_tools', 'candidate_tools', 'approved_tools'])
  );

  const toggleNode = (nodeId: string) => {
    setExpandedNodes(prev => {
      const next = new Set(prev);
      if (next.has(nodeId)) {
        next.delete(nodeId);
      } else {
        next.add(nodeId);
      }
      return next;
    });
  };

  const Card = ({ title, children, status }: { title: string, children: React.ReactNode, status?: string }) => (
    <div className="border border-gray-700/60 bg-black/40 mb-4 p-3 flex flex-col hover:border-gray-600 transition-colors shadow-sm">
      <div className="flex justify-between items-center border-b border-gray-700/40 pb-2 mb-3">
        <h3 className="text-white uppercase tracking-widest font-bold text-xs">{title}</h3>
        {status && <span className="text-[10px] text-gray-500 font-mono uppercase px-1 py-0.5 border border-gray-700/40">{status}</span>}
      </div>
      <div className="text-sm font-mono text-gray-400 flex-1 space-y-2">
        {children}
      </div>
    </div>
  );

  const toolsStructure = [
    {
      id: "agent_tools",
      name: "Agent Tools",
      children: [
        {
          id: "agent_tools_1",
          name: "Workspace Security Scan",
          children: [
            "Sensitive Info Leak Detector",
            "Malicious Skills Identifier",
            "Malicious Tools Identifier",
            "Prompt Injection Detector",
            "Permission Forgery Detector",
            "Suspicious Script Scanner",
            "High-Risk Op Trace Analyzer"
          ]
        },
        {
          id: "agent_tools_2",
          name: "Agent Risk Behavior Review",
          children: [
            "Sensitive Data Leak Detector",
            "Phishing Page Interceptor",
            "Risky Operation Blocker"
          ]
        },
        {
          id: "agent_tools_3",
          name: "Task Execution Analysis",
          children: [
            "Critical Atomic Step Executor",
            "Redundant Step Optimizer",
            "Task Planning Advisor",
            "Skills/Tools Usage Analyzer"
          ]
        }
      ]
    },
    { id: "score_tools", name: "Data Scoring Tools", children: [] },
    { id: "filter_tools", name: "Data Filtering Tools", children: [] },
    { id: "process_tools", name: "Data Processing Tools", children: [] },
    { id: "secure_tools", name: "Data Security Tools", children: [] },
    {
      id: "custom_tools",
      name: "Derived Tools",
      children: [
        {
          id: "candidate_tools",
          name: "Candidate Derived Tools",
          children: candidateTools || []
        },
        {
          id: "approved_tools",
          name: "Stable Derived Tools",
          children: [
            "policy_safe_semantic_risk_audit",
            "balanced_security_triage_bundle",
            "privacy_jailbreak_review_pack",
            "exec_review_brief_generator"
          ]
        }
      ]
    }
  ];

  return (
    <div className="w-80 border-l border-gray-900/40 bg-black/60 flex flex-col shrink-0 overflow-y-auto p-4 custom-scrollbar h-full">
      <Card title="Tools Catalog" status="14 Online">
        <div className="space-y-3 select-none">
          {toolsStructure.map(level2 => {
            const isLevel2Expanded = expandedNodes.has(level2.id);
            const hasChildren2 = level2.children.length > 0;
            
            return (
              <div key={level2.id} className="border border-gray-700/40 bg-black/60 flex flex-col transition-colors shadow-inner overflow-hidden">
                <div
                  className={cn(
                    "flex justify-between items-center p-2 cursor-pointer hover:bg-gray-900/20 group",
                    isLevel2Expanded ? "border-b border-gray-700/40 bg-gray-900/10" : ""
                  )}
                  onClick={() => toggleNode(level2.id)}
                >
                  <span className={cn(
                    "text-xs uppercase tracking-widest font-bold group-hover:text-white transition-colors",
                    isLevel2Expanded ? "text-white" : "text-gray-400"
                  )}>
                    {level2.name}
                  </span>
                  <span className="text-gray-600 text-[10px] font-mono group-hover:text-gray-400">
                    {hasChildren2 ? (isLevel2Expanded ? '[-]' : '[+]') : ''}
                  </span>
                </div>

                {isLevel2Expanded && hasChildren2 && (
                  <div className="p-2 space-y-2 bg-black/40">
                    {level2.children.map((level3: any) => {
                      const isLevel3Expanded = expandedNodes.has(level3.id);
                      const hasChildren3 = level3.children && level3.children.length > 0;

                      return (
                        <div key={level3.id} className="border border-gray-700/40 bg-black/80 flex flex-col hover:border-gray-600/60 transition-colors">
                          <div
                            className={cn(
                              "flex justify-between items-center p-1.5 cursor-pointer hover:bg-gray-900/30 group/sub",
                              isLevel3Expanded ? "border-b border-gray-700/40 bg-gray-900/20" : ""
                            )}
                            onClick={() => toggleNode(level3.id)}
                          >
                            <span className={cn(
                              "text-[11px] font-bold tracking-wider group-hover/sub:text-white",
                              isLevel3Expanded ? "text-white" : "text-gray-400"
                            )}>
                              {level3.name}
                            </span>
                            <span className="text-gray-600 text-[8px] font-mono group-hover/sub:text-gray-400">
                              {hasChildren3 ? (isLevel3Expanded ? '▼' : '▶') : ''}
                            </span>
                          </div>

                          {isLevel3Expanded && hasChildren3 && (
                            <div className="p-2 flex flex-wrap gap-1.5 bg-[#030703] shadow-[inset_0_2px_4px_rgba(0,0,0,0.5)]">
                              {level3.children.map((item: any, idx: number) => {
                                const isCandidateObj = typeof item === 'object' && item !== null;
                                const itemId = isCandidateObj ? item.id : item;
                                const itemName = isCandidateObj ? item.name : item;
                                const isHighlighted = activeTool === itemName;
                                
                                if (isCandidateObj) {
                                  return (
                                    <div 
                                      key={`${item.id}-${idx}`}
                                      onClick={() => item.status === 'pending' && item.sessionId && item.messageId && onCandidateToolClick?.(item.sessionId, item.messageId)}
                                      className={cn(
                                        "border px-2 py-1 text-[10px] flex items-center gap-1.5 shadow-sm transition-colors cursor-pointer shrink-0",
                                        item.status === 'pending'
                                          ? "border-red-500/60 bg-red-950/30 text-red-400 animate-pulse hover:bg-red-900/50"
                                          : "border-gray-400 bg-gray-900/40 text-gray-300"
                                      )}
                                      title={item.name}
                                    >
                                      <span className={cn(
                                        "w-1 h-1 rounded-full inline-block shrink-0",
                                        item.status === 'pending' ? "bg-red-400 shadow-[0_0_5px_rgba(248,113,113,0.8)]" : "bg-gray-400 shadow-[0_0_5px_rgba(74,222,128,0.8)]"
                                      )}></span>
                                      <div className="flex flex-col">
                                        <span>{item.name}</span>
                                        {item.status === 'pending' && <span className="text-[8px] opacity-70">Awaiting Approval</span>}
                                      </div>
                                    </div>
                                  );
                                }

                                return (
                                <div 
                                  key={item} 
                                  className={cn(
                                    "border px-2 py-1 text-[10px] cursor-pointer transition-all hover:-translate-y-px flex items-center gap-1 shadow-sm shrink-0",
                                    isHighlighted
                                      ? "border-white bg-gray-900/40 text-white animate-pulse"
                                      : "border-gray-700/40 bg-gray-950/20 text-gray-400 hover:bg-gray-800/50 hover:text-white hover:border-gray-500/50"
                                  )}
                                  title={item}
                                >
                                  <span className={cn(
                                    "w-1 h-1 rounded-full inline-block",
                                    isHighlighted ? "bg-white shadow-[0_0_5px_rgba(255,255,255,0.8)]" : "bg-gray-600/50"
                                  )}></span>
                                  {item}
                                </div>
                                );
                              })}
                            </div>
                          )}
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </Card>

      <Card title="Datasets" status="5 Loaded">
        <div className="space-y-3">
          {[
            { name: 'posttrain_dialog_safety_v3', size: '156 MB', rows: '420K', extra: 'Nesting: 5' },
            { name: 'instruction_tuning_value_pack', size: '89 MB', rows: '287K', extra: 'Nesting: 3' },
            { name: 'diverse_instruction_pickset', size: '234 MB', rows: '650K', extra: 'Nesting: 4' },
            { name: 'wind_tunnel_trajectory_archive', size: '512 MB', rows: '1.2M', extra: 'Nesting: 6' },
            { name: 'agent_trace_skill_mining_pool', size: '378 MB', rows: '980K', extra: 'Nesting: 7' },
          ].map((ds, idx) => (
            <div key={idx} className="hover:bg-gray-900/10 p-1 rounded-sm cursor-pointer border-l-2 border-gray-700 transition-colors">
              <div className="flex justify-between text-white">
                <span className="truncate pr-2">{ds.name}</span>
                <span className="text-[10px] shrink-0">{ds.size}</span>
              </div>
              <div className="text-[10px] text-gray-500 pt-1 font-mono">Rows: {ds.rows} | {ds.extra}</div>
            </div>
          ))}
          <div className="pt-2 border-t border-gray-700/30 text-center text-xs text-gray-500 hover:text-white cursor-pointer flex items-center justify-center gap-2 transition-colors">
            <span>[+] LOAD DATASET</span>
          </div>
        </div>
      </Card>

      <Card title="System Status" status="Nominal">
        <div className="space-y-3">
          <div className="space-y-1">
            <div className="flex justify-between text-xs text-gray-400 uppercase">
              <span>CPU</span>
              <span className="text-cyan-400">12%</span>
            </div>
            <div className="h-1.5 w-full bg-gray-950 rounded-full overflow-hidden">
              <div className="h-full bg-cyan-500 w-[12%] animate-pulse"></div>
            </div>
          </div>
          <div className="space-y-1">
            <div className="flex justify-between text-xs text-gray-400 uppercase">
              <span>Memory</span>
              <span className="text-yellow-400">45% (8.2/16GB)</span>
            </div>
            <div className="h-1.5 w-full bg-gray-950 rounded-full overflow-hidden">
              <div className="h-full bg-yellow-500 w-[45%]"></div>
            </div>
          </div>
          <div className="space-y-1">
            <div className="flex justify-between text-xs text-gray-400 uppercase">
              <span>Storage</span>
              <span className="text-white">72%</span>
            </div>
            <div className="h-1.5 w-full bg-gray-950 rounded-full overflow-hidden">
              <div className="h-full bg-white w-[72%]"></div>
            </div>
          </div>
          <div className="flex justify-between text-xs pt-2 border-t border-gray-700/30">
            <span className="text-gray-500">Network:</span>
            <span className="text-white">12ms Ping / 45Mb/s</span>
          </div>
        </div>
      </Card>
    </div>
  );
}
