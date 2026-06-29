export function buildModeCommandPromptMessage(sessionId, mode, timestamp = currentTime()) {
  const normalizedMode = String(mode || 'run').toLowerCase() === 'pilot' ? 'pilot' : 'run';
  const title = normalizedMode === 'pilot' ? 'PILOT MODE READY' : 'RUN MODE READY';
  const detail = normalizedMode === 'pilot'
    ? '请输入 pilot 任务命令，例如：elf pilot "run security_audit on security_audit_samples"。'
    : '请输入 run 任务命令，例如：elf run "run security_audit on security_audit_samples"。';

  return {
    id: `${sessionId}-mode-command-prompt`,
    type: 'system',
    content: `${title}\n\n${detail}\n也可以直接输入自然语言任务。`,
    timestamp,
  };
}

function currentTime() {
  return new Date().toLocaleTimeString('en-US', { hour12: false });
}
