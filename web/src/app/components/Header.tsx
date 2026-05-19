import { useEffect, useState } from 'react';
import { cn } from '../utils';

export function Header({ mode = 'NA', score = 'NA', sessionId = '0x0000', sessionName = 'Untitled', status = 'IDLE' }: { mode?: string, score?: number | 'NA', sessionId?: string, sessionName?: string, status?: string }) {
  const [elapsed, setElapsed] = useState(0);
  const [animateScore, setAnimateScore] = useState(false);

  useEffect(() => {
    if (score !== 'NA') {
      setAnimateScore(true);
      const t = setTimeout(() => setAnimateScore(false), 500);
      return () => clearTimeout(t);
    }
  }, [score]);

  useEffect(() => {
    const timer = setInterval(() => {
      setElapsed((prev) => prev + 1);
    }, 1000);
    return () => clearInterval(timer);
  }, []);

  const formatTime = (seconds: number) => {
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    return `${h.toString().padStart(2, '0')}:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
  };

  const statClass = "flex items-center gap-2 px-3 py-1 bg-black/40 border border-gray-700/40";
  const labelClass = "text-gray-500 text-xs uppercase tracking-wider";
  const valueClass = "text-white font-mono font-medium";

  return (
    <header className="flex items-center justify-between p-2 border-b border-gray-700/60 bg-black text-sm shrink-0 flex-wrap gap-2">
      <div className="flex items-center gap-4">
        <h1 className="text-xl font-bold tracking-tight text-white flex items-center gap-2 uppercase">
          <span className="text-gray-500">#</span> DataElf
        </h1>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <div className={statClass}>
          <span className={labelClass}>Session</span>
          <span className={valueClass}>#{sessionId} <span className="text-cyan-400 uppercase truncate ml-2 max-w-[120px] inline-block align-bottom">{sessionName}</span></span>
        </div>
        <div className={statClass}>
          <span className={labelClass}>Mode</span>
          <span className={cn(valueClass, "text-cyan-400 uppercase")}>{mode}</span>
        </div>
        <div className={statClass}>
          <span className={labelClass}>Status</span>
          <span className="flex items-center gap-1.5">
            <span className={cn(
              "h-2 w-2 rounded-full animate-pulse",
              status === 'PENDING' ? "bg-red-500" : (status === 'IDLE' ? "bg-gray-600" : "bg-green-500")
            )}></span>
            <span className={cn(
              valueClass, 
              status === 'PENDING' ? "text-red-500 animate-pulse font-bold" : "animate-pulse"
            )}>
              {status}
            </span>
          </span>
        </div>
        <div className={statClass}>
          <span className={labelClass}>Model</span>
          <span className={cn(valueClass, "text-yellow-400")}>dataelf-7B</span>
        </div>
        <div className={statClass}>
          <span className={labelClass}>Elapsed</span>
          <span className={valueClass}>{formatTime(elapsed)}</span>
        </div>
        <div className={statClass}>
          <span className={labelClass}>Score</span>
          <span className={cn(valueClass, animateScore && "scale-125 font-bold text-yellow-300 transition-all duration-300 inline-block")}>
            {score === 'NA' ? 'NA' : score.toFixed(2)}
          </span>
        </div>
      </div>
    </header>
  );
}