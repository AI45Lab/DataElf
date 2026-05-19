import { HTMLAttributes } from 'react';
import { cn } from '../utils';

interface TerminalContainerProps extends HTMLAttributes<HTMLDivElement> {
  title?: string;
}

export function TerminalContainer({ children, title, className, ...props }: TerminalContainerProps) {
  return (
    <div
      className={cn(
        "border border-gray-700/40 bg-black/60 flex flex-col relative",
        className
      )}
      {...props}
    >
      {title && (
        <div className="bg-gray-900/20 text-white border-b border-gray-700/40 px-3 py-1 text-xs font-bold uppercase tracking-wider shrink-0 flex items-center justify-between">
          <span>{title}</span>
        </div>
      )}
      <div className="flex-1 overflow-auto overflow-x-hidden relative scrollbar-hide">
        {children}
      </div>
    </div>
  );
}
