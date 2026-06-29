import { useState } from 'react';
import { cn } from '../utils';

interface CandidateTool {
  id: string;
  name: string;
  status: 'pending' | 'stable';
  sessionId?: string;
  messageId?: string;
}

interface DatasetCatalogItem {
  name: string;
  rows?: string | number;
  nesting?: string | number;
  size?: string;
}

interface ToolCatalogItem {
  name: string;
  description?: string;
  parameters?: {
    properties?: Record<string, unknown>;
  };
}

export function RightSidebar({
  activeTool,
  candidateTools,
  datasets = [],
  tools = [],
  onCandidateToolClick
}: {
  activeTool?: string | null,
  candidateTools?: CandidateTool[] | null,
  datasets?: DatasetCatalogItem[],
  tools?: ToolCatalogItem[],
  onCandidateToolClick?: (sessionId: string, messageId: string) => void
}) {
  const [expandedNodes, setExpandedNodes] = useState<Set<string>>(
    new Set(['derived_tools', 'candidate_tools', 'approved_tools'])
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

  const stableDerivedTools = [
    'policy_safe_semantic_risk_audit',
    'balanced_security_triage_bundle',
    'privacy_jailbreak_review_pack',
    'exec_review_brief_generator'
  ];

  const renderToolChip = (item: string | CandidateTool, idx: number) => {
    const isCandidateObj = typeof item === 'object' && item !== null;
    const itemName = isCandidateObj ? item.name : item;
    const isHighlighted = activeTool === itemName;

    if (isCandidateObj) {
      return (
        <div
          key={`${item.id}-${idx}`}
          onClick={() => item.status === 'pending' && item.sessionId && item.messageId && onCandidateToolClick?.(item.sessionId, item.messageId)}
          className={cn(
            "border px-2 py-1 text-[10px] flex items-center gap-1.5 shadow-sm transition-colors shrink-0",
            item.status === 'pending'
              ? "border-red-500/60 bg-red-950/30 text-red-400 animate-pulse hover:bg-red-900/50 cursor-pointer"
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
        key={itemName}
        className={cn(
          "border px-2 py-1 text-[10px] transition-all hover:-translate-y-px flex items-center gap-1 shadow-sm shrink-0",
          isHighlighted
            ? "border-white bg-gray-900/40 text-white animate-pulse"
            : "border-gray-700/40 bg-gray-950/20 text-gray-400 hover:bg-gray-800/50 hover:text-white hover:border-gray-500/50"
        )}
        title={itemName}
      >
        <span className={cn(
          "w-1 h-1 rounded-full inline-block",
          isHighlighted ? "bg-white shadow-[0_0_5px_rgba(255,255,255,0.8)]" : "bg-gray-600/50"
        )}></span>
        {itemName}
      </div>
    );
  };

  const renderDerivedGroup = (id: string, name: string, children: Array<string | CandidateTool>) => {
    const isExpanded = expandedNodes.has(id);
    return (
      <div key={id} className="border border-gray-700/40 bg-black/80 flex flex-col hover:border-gray-600/60 transition-colors">
        <div
          className={cn(
            "flex justify-between items-center p-1.5 cursor-pointer hover:bg-gray-900/30 group/sub",
            isExpanded ? "border-b border-gray-700/40 bg-gray-900/20" : ""
          )}
          onClick={() => toggleNode(id)}
        >
          <span className={cn(
            "text-[11px] font-bold tracking-wider group-hover/sub:text-white",
            isExpanded ? "text-white" : "text-gray-400"
          )}>
            {name}
          </span>
          <span className="text-gray-600 text-[8px] font-mono group-hover/sub:text-gray-400">
            {children.length > 0 ? (isExpanded ? '[-]' : '[+]') : ''}
          </span>
        </div>

        {isExpanded && children.length > 0 && (
          <div className="p-2 flex flex-wrap gap-1.5 bg-[#030703] shadow-[inset_0_2px_4px_rgba(0,0,0,0.5)]">
            {children.map(renderToolChip)}
          </div>
        )}
      </div>
    );
  };

  return (
    <div className="w-80 border-l border-gray-900/40 bg-black/60 flex flex-col shrink-0 overflow-y-auto p-4 custom-scrollbar h-full">
      <Card title="Tools Catalog" status={`${tools.length} Online`}>
        <div className="space-y-3 select-none">
          {tools.map(tool => {
            const nodeId = `tool_${tool.name}`;
            const isExpanded = expandedNodes.has(nodeId);
            const parameterNames = Object.keys(tool.parameters?.properties || {});
            return (
              <div key={tool.name} className="border border-gray-700/40 bg-black/60 flex flex-col transition-colors shadow-inner overflow-hidden">
                <div
                  className={cn(
                    "flex justify-between items-center p-2 cursor-pointer hover:bg-gray-900/20 group",
                    isExpanded ? "border-b border-gray-700/40 bg-gray-900/10" : ""
                  )}
                  onClick={() => toggleNode(nodeId)}
                >
                  <span className={cn(
                    "text-xs uppercase tracking-widest font-bold group-hover:text-white transition-colors",
                    isExpanded ? "text-white" : "text-gray-400"
                  )}>
                    {tool.name}
                  </span>
                  <span className="text-gray-600 text-[10px] font-mono group-hover:text-gray-400">
                    {isExpanded ? '[-]' : '[+]'}
                  </span>
                </div>

                {isExpanded && (
                  <div className="p-2 bg-black/40 space-y-2">
                    {tool.description && (
                      <div className="text-[10px] text-gray-500 leading-relaxed">
                        {tool.description}
                      </div>
                    )}
                    {parameterNames.length > 0 && (
                      <div className="flex flex-wrap gap-1.5">
                        {parameterNames.map(param => (
                          <span key={param} className="border border-gray-700/40 bg-gray-950/30 px-1.5 py-0.5 text-[9px] text-gray-400">
                            {param}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </div>
            );
          })}

          {tools.length === 0 && (
            <div className="text-xs text-gray-600 text-center py-4 uppercase tracking-widest font-mono">
              No backend tools loaded
            </div>
          )}

          <div className="border border-gray-700/40 bg-black/60 flex flex-col transition-colors shadow-inner overflow-hidden">
            <div
              className={cn(
                "flex justify-between items-center p-2 cursor-pointer hover:bg-gray-900/20 group",
                expandedNodes.has('derived_tools') ? "border-b border-gray-700/40 bg-gray-900/10" : ""
              )}
              onClick={() => toggleNode('derived_tools')}
            >
              <span className={cn(
                "text-xs uppercase tracking-widest font-bold group-hover:text-white transition-colors",
                expandedNodes.has('derived_tools') ? "text-white" : "text-gray-400"
              )}>
                Derived Tools
              </span>
              <span className="text-gray-600 text-[10px] font-mono group-hover:text-gray-400">
                {expandedNodes.has('derived_tools') ? '[-]' : '[+]'}
              </span>
            </div>

            {expandedNodes.has('derived_tools') && (
              <div className="p-2 space-y-2 bg-black/40">
                {renderDerivedGroup('candidate_tools', 'Candidate Derived Tools', candidateTools || [])}
                {renderDerivedGroup('approved_tools', 'Stable Derived Tools', stableDerivedTools)}
              </div>
            )}
          </div>
        </div>
      </Card>

      <Card title="Datasets" status={`${datasets.length} Loaded`}>
        <div className="space-y-3">
          {datasets.map((ds) => (
            <div key={ds.name} className="p-1 border-l-2 border-gray-700 transition-colors">
              <div className="flex justify-between text-white">
                <span className="truncate pr-2">{ds.name}</span>
                <span className="text-[10px] shrink-0">{ds.size || 'NA'}</span>
              </div>
              <div className="text-[10px] text-gray-500 pt-1 font-mono">Rows: {ds.rows ?? 'NA'} | Nesting: {ds.nesting ?? 'NA'}</div>
            </div>
          ))}
          {datasets.length === 0 && (
            <div className="text-xs text-gray-600 text-center py-4 uppercase tracking-widest font-mono">
              No datasets loaded
            </div>
          )}
          <div className="pt-2 border-t border-gray-700/30 text-center text-xs text-gray-600 flex items-center justify-center gap-2 cursor-not-allowed opacity-60">
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
