/** Service-only protocol selection from explicit configuration/registry metadata.
 * Model names are never used to infer a wire grammar. A native override wins.
 */
export function resolveToolProtocol(configured: string | undefined, model: any): 'native' | 'glm_text' {
    const policy = configured || 'model_compat';
    if (!['model_compat', 'native', 'glm_text'].includes(policy)) throw new Error(`Unsupported server tool protocol policy: ${policy}`);
    const resolved = policy === 'model_compat'
        ? (model?.api === 'openai-completions' && model.compat?.thinkingFormat === 'zai' ? 'glm_text' : 'native')
        : policy;
    if (resolved === 'glm_text' && model?.api !== 'openai-completions') {
        throw new Error('Server glm_text protocol requires an openai-completions model');
    }
    return resolved as 'native' | 'glm_text';
}
