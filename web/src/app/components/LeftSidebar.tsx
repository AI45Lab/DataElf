import { useState } from 'react';
import { cn } from '../utils';

interface Session {
  id: string;
  name: string;
  date: string;
  active: boolean;
}

interface LeftSidebarProps {
  sessions: Session[];
  onNewSession: () => void;
  onSelectSession: (id: string) => void;
  onDeleteSession: (id: string) => void;
}

export function LeftSidebar({ sessions, onNewSession, onSelectSession, onDeleteSession }: LeftSidebarProps) {
  const [collapsed, setCollapsed] = useState(false);

  const handleDelete = (e: React.MouseEvent, id: string) => {
    e.stopPropagation();
    onDeleteSession(id);
  };

  if (collapsed) {
    return (
      <div className="w-12 border-r border-gray-700/40 bg-black/60 flex flex-col items-center py-2 shrink-0">
        <button
          onClick={() => setCollapsed(false)}
          className="text-gray-400 hover:text-white p-2 hover:bg-gray-900/20"
          title="Expand Sessions"
        >
          {`>>`}
        </button>
      </div>
    );
  }

  return (
    <div className="w-64 border-r border-gray-700/40 bg-black/60 flex flex-col shrink-0 h-full">
      <div className="flex items-center justify-between p-2 border-b border-gray-700/40 text-xs text-white uppercase tracking-widest font-bold">
        <span>History</span>
        <button
          onClick={() => setCollapsed(true)}
          className="hover:text-gray-300 hover:bg-gray-900/20 px-1"
        >
          {`<<`}
        </button>
      </div>
      
      <div className="p-3">
        <button
          onClick={onNewSession}
          className="w-full text-left bg-gray-800/30 hover:bg-gray-700 text-white border border-gray-600/50 p-2 text-sm uppercase font-bold tracking-wider mb-4 flex items-center justify-between transition-colors"
        >
          <span>New Session</span>
          <span className="text-gray-300 font-mono">[+]</span>
        </button>

        <div className="space-y-1 overflow-y-auto max-h-[calc(100vh-140px)] custom-scrollbar pr-1">
          {sessions.map((session) => (
            <div
              key={session.id}
              onClick={() => onSelectSession(session.id)}
              className={cn(
                "group w-full text-left text-sm px-2 py-1.5 flex flex-col gap-1 border-l-2 hover:bg-gray-900/20 cursor-pointer transition-colors",
                session.active
                  ? "border-white text-white bg-gray-900/10"
                  : "border-transparent text-gray-500 hover:text-gray-300"
              )}
            >
              <div className="flex justify-between items-center w-full">
                <span className="font-mono truncate">{session.name}</span>
                <div className="flex items-center gap-1.5 shrink-0">
                  <button 
                    onClick={(e) => handleDelete(e, session.id)}
                    className="opacity-0 group-hover:opacity-100 text-red-600/80 hover:text-red-400 hover:bg-red-900/30 text-[10px] font-bold px-1 py-0.5 transition-opacity border border-transparent hover:border-red-900/50 rounded-sm uppercase tracking-wider"
                    title="Delete Session"
                  >
                    DEL
                  </button>
                  {session.active && <span className="text-xs text-black bg-white px-1 font-bold animate-pulse">&lt;</span>}
                </div>
              </div>
              <div className="flex justify-between w-full text-xs font-mono opacity-70">
                <span>{session.id}</span>
                <span>{session.date}</span>
              </div>
            </div>
          ))}
          {sessions.length === 0 && (
            <div className="text-xs text-gray-600 text-center py-4 uppercase tracking-widest font-mono">
              No History Found
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
