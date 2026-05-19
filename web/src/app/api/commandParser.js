export function parseUserCommand(command) {
  const rawCommand = command.trim();
  if (!rawCommand) {
    return { rawCommand, mode: 'RUN', task: '' };
  }

  const tokens = splitCommand(rawCommand);
  const first = (tokens[0] || '').toLowerCase();
  if (first === 'elf' && tokens.length > 1) {
    const mode = tokens[1].toLowerCase();
    if (isKnownMode(mode)) {
      return {
        rawCommand,
        mode: mode.toUpperCase(),
        task: tokens.slice(2).join(' ').trim(),
      };
    }
  }

  if (isKnownMode(first)) {
    return {
      rawCommand,
      mode: first.toUpperCase(),
      task: tokens.slice(1).join(' ').trim(),
    };
  }

  return { rawCommand, mode: 'RUN', task: rawCommand };
}

function isKnownMode(value) {
  return value === 'run' || value === 'pilot' || value === 'submit';
}

function splitCommand(command) {
  const tokens = [];
  const pattern = /"([^"]*)"|'([^']*)'|(\S+)/g;
  let match;
  while ((match = pattern.exec(command)) !== null) {
    tokens.push(match[1] ?? match[2] ?? match[3]);
  }
  return tokens.length ? tokens : command.split(/\s+/);
}
