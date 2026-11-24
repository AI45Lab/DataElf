/** Only specific trusted backend tools may hand control to the host. */
export function trustedHandoff(name:string, result:any, role:string):boolean {
 const receipt=result?.control_event;
 if(receipt?.schema_version!=='dataelf.tool-control.v1' || !receipt.binding || typeof receipt.binding!=='string')return false;
 const reviewer=role.endsWith('reviewer');
 const allowed:Record<string,string[]> = reviewer ? {
  analysis_review_submit:['review_submitted'],ontology_submit:['review_submitted'],review_feedback_submit:['local_feedback']
 } : {analysis_submit:['delivery_submitted'],ontology_submit:['candidate_submitted'],
  ontology_draft:['candidate_submitted'],graph_commit:['candidate_submitted','graph_review'],review_request:['local_review']};
 return !!allowed[name]?.includes(receipt.kind);
}
