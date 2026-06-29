const TYPE_RANKS = {
  clarification: 10,
  pipeline: 20,
  execution: 30,
  lifecycle: 40,
  pilot_summary: 50,
  tool_candidate: 60,
  pipeline_candidate: 60,
  result: 70,
};

export function isJobMessage(message, jobId) {
  if (!message || !jobId) return false;
  if (message.jobId === jobId) return true;
  if (message.summaryData?.jobId === jobId) return true;
  if (message.resultData?.jobId === jobId) return true;
  return String(message.id || '').startsWith(`${jobId}-`);
}

export function upsertJobMessages(existingMessages, jobId, incomingMessages) {
  const incoming = Array.isArray(incomingMessages) ? incomingMessages : [incomingMessages];
  const byId = new Map();
  const originalOrder = new Map();
  const merged = [];

  existingMessages.forEach((message, index) => {
    byId.set(message.id, message);
    originalOrder.set(message.id, index);
    merged.push(message);
  });

  incoming.filter(Boolean).forEach((message) => {
    if (byId.has(message.id)) {
      const index = merged.findIndex(item => item.id === message.id);
      merged[index] = { ...byId.get(message.id), ...message };
      byId.set(message.id, merged[index]);
      return;
    }
    originalOrder.set(message.id, existingMessages.length + originalOrder.size);
    byId.set(message.id, message);
    merged.push(message);
  });

  return orderJobMessages(merged, jobId, originalOrder);
}

export function orderJobMessages(messages, jobId, originalOrder = null) {
  const firstJobIndex = messages.findIndex(message => isJobMessage(message, jobId));
  if (firstJobIndex === -1) return messages;

  const jobMessages = [];
  const nonJobMessages = [];
  messages.forEach((message, index) => {
    if (isJobMessage(message, jobId)) {
      jobMessages.push({ message, index });
    } else {
      nonJobMessages.push({ message, index });
    }
  });

  jobMessages.sort((left, right) => {
    const rankDiff = messageRank(left.message) - messageRank(right.message);
    if (rankDiff !== 0) return rankDiff;
    const leftOrder = originalOrder?.get(left.message.id) ?? left.index;
    const rightOrder = originalOrder?.get(right.message.id) ?? right.index;
    return leftOrder - rightOrder;
  });

  const before = nonJobMessages
    .filter(item => item.index < firstJobIndex)
    .map(item => item.message);
  const after = nonJobMessages
    .filter(item => item.index >= firstJobIndex)
    .map(item => item.message);

  return [
    ...before,
    ...jobMessages.map(item => item.message),
    ...after,
  ];
}

function messageRank(message) {
  return TYPE_RANKS[message.type] ?? 90;
}
