/** Process-local final request policy. Registry metadata is not an explicit user cap. */
export function outputLimit(payload:any, limit:any, model?:any):any {
 const result={...payload};
 for(const name of ['max_tokens','max_completion_tokens','max_output_tokens'])delete result[name];
 if(limit!==undefined && limit!==null){
  if(!Number.isInteger(limit) || limit<=0)throw new Error('Output token limit must be a positive integer');
  const field=model?.api==='openai-responses' ? 'max_output_tokens' : model?.api==='anthropic-messages' ? 'max_tokens' : model?.compat?.maxTokensField || 'max_tokens';
  result[field]=limit;
 }
 return result;
}
