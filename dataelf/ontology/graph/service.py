"""Shared graph API for modeling, review, and analysis without Pi dependencies."""
import json
import shutil
import uuid
from copy import deepcopy
from pathlib import Path
from rdflib import Dataset,Graph,URIRef,BNode,RDF
from .contracts import GraphRef,Quad,ChangeSet,CommitResult,Evidence,GraphDiff
from .store import GraphStore,decode_state,encode_dataset,content_hash,canonical_quads,write_json,parse_nquads
from .provenance import fact_id,check_evidence,support
from .validation import validate,shapes
from .query import execute,view,validate_query,construct_page
from ..contracts import OntologyContract,SourceInput,Review
from ..sources import SourceCatalog,digest,file_hash
from ..engine import materialize,iri


def invalidate_template(state,previous,dataset):
    if state.get('template') and (state.get('contract')!=previous.get('contract') or
            content_hash(dataset)!=content_hash(decode_state(previous))):
        state['template']=None
        state.setdefault('metadata',{})['templateSaved']=False
        state['template_export_status']='Candidate RDF or contract changed; reusable export requires a new replay check'

class GraphService:
    def __init__(self,root,evidence_root=None):
        self.store=GraphStore(root);self.root=self.store.root
        self.evidence_root=Path(evidence_root or self.root.parent.parent).resolve()
    fact_id=staticmethod(fact_id)
    def ref(self,ref):return GraphRef.model_validate(ref) if isinstance(ref,dict) else ref
    def state(self,ref):return self.store.load(self.ref(ref))
    def dataset(self,ref):return decode_state(self.state(ref))
    def binding(self,state):
        return digest({'graph':content_hash(decode_state(state)), 'contract':state.get('contract'),
                       'sources':state.get('source_hashes'), 'provenance':state['provenance'],
                       'changes':state['changes'],'materialization_issues':state.get('materialization_issues',[]),
                       'graph_names':state['graph_names'],'inference_graphs':state['inference_graphs'],
                       'review_policy':state['review_policy']})
    def workspace(self,action,*,graph_id=None,base=None,workspace_id=None,review_policy=None):
        if base:base=self.ref(base);graph_id=base.graph_id
        if action=='open':
            ref=GraphRef(graph_id=graph_id,workspace_id=workspace_id);self.state(ref);return ref
        if action=='discard':
            ref=GraphRef(graph_id=graph_id,workspace_id=workspace_id)
            with self.store.lock(graph_id):shutil.rmtree(self.store.path(ref))
            return {'discarded':True}
        if action!='create':raise ValueError('unknown workspace action')
        if review_policy is not None and review_policy not in {'required','deterministic'}:raise ValueError('unknown review policy')
        ref=GraphRef(graph_id=graph_id or uuid.uuid4().hex,workspace_id=uuid.uuid4().hex)
        with self.store.lock(ref.graph_id):
            if base:
                if not base.revision_id:raise ValueError('workspace base must be an immutable revision')
                state=deepcopy(self.state(base));state['base_revision']=base.revision_id
                state['review_policy']=review_policy or 'required'
            else:
                state={'base_revision':None,'quads':[],'contract':None,'source_inputs':[], 'source_hashes':{},
                       'provenance':{'facts':{},'activities':[]},'provenance_precision':'fact', 'changes':[],
                       'graph_names':{},'inference_graphs':['urn:g:inference'],'review_policy':review_policy or 'required','materialization_issues':[]}
            state['review']=None;state['commits']={};self.store.save(ref,state)
        return ref
    def require_review(self,ref):
        """Trusted submission coordinator can elevate existing candidates."""
        ref=self.ref(ref)
        if not ref.workspace_id:raise ValueError('maintenance needs a candidate workspace')
        with self.store.lock(ref.graph_id):
            state=self.state(ref)
            if state['review_policy']!='required':
                state['review_policy']='required';state['review']=None;self.store.save(ref,state)
    def inspect(self,ref,resource='summary',offset=0,limit=100):
        state=self.state(ref);ds=decode_state(state)
        summary={'graph_ref':self.ref(ref).model_dump(mode='json'),'base_revision':state['base_revision'],
            'base_graph_ref':{'graph_id':self.ref(ref).graph_id,'revision_id':state['base_revision']} if state['base_revision'] else None,
            'candidate_hash':self.binding(state),'content_hash':content_hash(ds),'quad_count':len(state['quads']),
            'named_graphs':sorted({q['graph'] for q in state['quads']}),'default_view':'current schema/domain/observation/source; excludes inference',
            'provenance_precision':state['provenance_precision'],'review_policy':state['review_policy'],'inference_graphs':state['inference_graphs']}
        if resource=='summary':return summary
        if resource not in {'contract','provenance','changes','validation','review','quads','review_context'}:raise ValueError('unknown resource')
        if resource=='review_context':return self.review_context(ref)
        if resource=='validation':return self.validate(ref).model_dump(mode='json')
        from ..resources import json_page
        value=state.get(resource);extra={}
        if resource=='provenance':
            current={fact_id(Quad.model_validate(q)) for q in state['quads']}
            value={**state['provenance'],'facts':{fid:{**item,'present':fid in current,
                'assertion_status':'asserted' if fid in current else 'retracted'} for fid,item in state['provenance']['facts'].items()}}
            extra={'asserted_fact_count':len(current),'retracted_record_count':sum(fid not in current for fid in value['facts']),
                'semantics':'Current assertions are Dataset quads only. Retracted provenance records are retained audit history, not current facts. Empty supports on a retracted record are intentional; active facts still require evidence.'}
        if resource=='changes':extra={'semantics':'Cumulative version history; earlier operations describe earlier revisions, not simultaneous current assertions.'}
        return {'resource':resource,**extra,**json_page(value,offset, min(16000,max(512,limit*100)))}
    def review_context(self,ref,limit=50):
        """Compact evidence entry point; deterministic validation remains global."""
        state=self.state(ref);reference=self.ref(ref);base_ref=None;base=None
        if state['base_revision']:
            base_ref=GraphRef(graph_id=reference.graph_id,revision_id=state['base_revision']);base=self.state(base_ref)
        before=canonical_quads(decode_state(base)) if base else set()
        after=canonical_quads(decode_state(state));added=after-before;removed=before-after
        entities={str(q[i]) for q in added|removed for i in (0,2) if isinstance(q[i],(URIRef,BNode))}
        classes={str(q[2]) for q in added|removed|after if q[1]==RDF.type and str(q[0]) in entities}
        contract=state.get('contract') or {};evidence=[]
        schema=set(state['graph_names'].get(k) for k in ['schema'])
        business_added={q for q in added if str(q[3]) not in schema};business_removed={q for q in removed if str(q[3]) not in schema}
        for quads,owner in [(business_added,state),(business_removed,base or state)]:
            for q in sorted(quads,key=str)[:limit]:
                for support in owner['provenance']['facts'].get(fact_id(Quad.from_nodes(*q)),{}).get('supports',[]):
                    item={k:support[k] for k in ('file','pointer','mapping','query','fact_ids') if support.get(k) is not None}
                    if item.get('file') or item.get('fact_ids'):
                        if item not in evidence:evidence.append(item)
        changed_properties={str(q[1]) for q in added|removed}
        constraints=contract.get('constraints',[])
        return {'candidate_hash':self.binding(state),'base_graph_ref':base_ref.model_dump(mode='json',exclude_none=True) if base_ref else None,
            'contract_changed':not base or base.get('contract')!=state.get('contract'),
            'added_count':len(added),'removed_count':len(removed),'truncated':len(business_added)>limit or len(business_removed)>limit,
            'schema_added_count':len(added-business_added),'schema_removed_count':len(removed-business_removed),
            'added':[Quad.from_nodes(*q).model_dump(mode='json') for q in sorted(business_added,key=str)[:limit]],
            'removed':[Quad.from_nodes(*q).model_dump(mode='json') for q in sorted(business_removed,key=str)[:limit]],
            'classes':contract.get('classes',{}) if not base or base.get('contract')!=state.get('contract') else {},
            'affected_entities':sorted(entities),'evidence':evidence,
            'properties':{key:value for key,value in contract.get('properties',{}).items() if str(iri(contract.get('namespace',''),key)) in changed_properties},
            'constraints':[c for c in constraints if str(iri(contract.get('namespace',''),c['class_id'])) in classes],
            'business_queries':contract.get('queries',[]),
            'semantics':'Review current changes and affected evidence/constraints. Historical withdrawn records are audit history. Full graph/contract/evidence tools remain available; global validation is always required.'}
    def read_graphs(self,state,graphs=None,include_inference=False):
        available={q['graph'] for q in state['quads']}
        if graphs is not None:
            if not set(graphs)<=available|set(state['graph_names'].values())|set(state['inference_graphs']):raise ValueError('unknown read graph')
            return graphs
        return sorted(available-(set() if include_inference else set(state['inference_graphs'])))
    def analysis_dataset(self,ref,graphs=None,include_inference=False):
        """Stable analysis view; historical revisions are separate references."""
        state=self.state(ref)
        return view(decode_state(state),self.read_graphs(state,graphs,include_inference))
    def query(self,ref,sparql,graphs=None,include_inference=False,offset=0,limit=100,timeout=10,cursor=None):
        state=self.state(ref);scope=self.read_graphs(state,graphs,include_inference)
        if validate_query(sparql)[1].name=='ConstructQuery':
            metadata={'graph_ref':self.ref(ref).model_dump(mode='json',exclude_none=True),'candidate_hash':self.binding(state),
                'query':sparql,'read_graphs':sorted(scope),'include_inference':include_inference,'explicit_graphs':sorted(graphs) if graphs is not None else None}
            identity=digest({k:v for k,v in metadata.items() if k!='explicit_graphs'})
            result=construct_page(view(decode_state(state),scope),sparql,self.root,identity,metadata,offset,limit,timeout,cursor)
        else:
            if cursor:raise ValueError('snapshot cursors currently apply to CONSTRUCT only')
            result=execute(view(decode_state(state),scope),sparql,offset=offset,limit=limit,timeout=timeout)
        return {**result,'graph_ref':self.ref(ref).model_dump(mode='json'),'read_graphs':result.get('read_graphs',scope),'query':sparql,
                'provenance_note':'query execution record; not a per-fact derivation proof'}
    def explore(self,ref,*,seeds,graphs=None,mode='neighborhood',**kwargs):
        state=self.state(ref)
        if mode=='provenance':
            ids={fact_id(Quad.model_validate(q)) for q in state['quads'] if q['subject']['value'] in seeds or q['object']['value'] in seeds}
            return {'facts':{i:state['provenance']['facts'].get(i) for i in ids},'precision':state['provenance_precision']}
        from .explore import explore
        return explore(view(decode_state(state),self.read_graphs(state,graphs)),seeds,mode=mode,**kwargs)
    def materialize(self,ref,contract,sources,*,replay_changes=True,agent_id=None,run_id=None,require_valid=False):
        ref=self.ref(ref)
        if not ref.workspace_id:raise ValueError('materialize needs a workspace')
        contract=OntologyContract.model_validate(contract) if isinstance(contract,dict) else contract
        sources=[SourceInput.model_validate(s) if isinstance(s,dict) else s for s in sources]
        catalog=SourceCatalog(self.evidence_root,sources)
        ds,lineage,validation=materialize(contract,catalog)
        with self.store.lock(ref.graph_id):
            old=self.state(ref);state=deepcopy(old);state.update(quads=encode_dataset(ds),contract=contract.model_dump(mode='json'),source_inputs=[s.model_dump(mode='json') for s in sources],
                source_hashes=catalog.hashes,provenance={'facts':{},'activities':[]},projection_lineage=lineage,
                materialization_issues=validation.get('issues',[]),provenance_precision='fact',graph_names={k:str(iri(contract.namespace,v)) for k,v in contract.graphs.items()},
                inference_graphs=[str(iri(contract.namespace,contract.graphs.get('inference','graph/inference')))],review=None)
            from ..capabilities import skill_review
            if skill_review(state.get('metadata',{})):
                for source in sources:
                    relative=source.metadata.get('query_record_file')
                    if relative:
                        path=(self.evidence_root/relative).resolve();expected=source.metadata['query_record_hash']
                        if not path.is_relative_to(self.evidence_root) or file_hash(path)!=expected:
                            raise ValueError('Source query metadata changed')
                        state['source_hashes'][relative]=expected
            state['provenance']['activities']=deepcopy(old['provenance']['activities'])
            activity={'id':uuid.uuid4().hex,'kind':'materialize','agent_id':agent_id,'run_id':run_id,'contract_hash':digest(state['contract'])}
            state['provenance']['activities'].append(activity)
            for row in lineage.get('facts',[]):
                quad=Quad.model_validate(row['quad']);e=Evidence(file=row['file'],pointer=row['pointer'],mapping=row['mapping'],source_id=row['source'])
                support(state,quad,[e],activity['id'])
            # Schema statements describe the explicit contract; they have their own activity.
            for q in ds.quads((None,None,None,None)):
                if str(q[3])==state['graph_names']['schema']:
                    state['provenance']['facts'][fact_id(Quad.from_nodes(*q))]={'quad':Quad.from_nodes(*q).model_dump(mode='json'),'supports':[{'activity':activity['id'],'source_id':'contract'}],'inference':False,'stale':False}
            state['changes']=[]
            if replay_changes:
                from .update import apply
                for saved in old['changes']:
                    c=ChangeSet.model_validate(saved['change'])
                    if c.expected_hash:raise ValueError('maintenance replay conflict: version-bound expected_hash requires explicit rebasing; the prior candidate is unchanged')
                    from .replay import verify_replay
                    verify_replay(saved,old,state,self.evidence_root)
                    check_evidence(c.evidence,self.evidence_root,state)
                    try:ds=apply(state,ds,c,saved['activity'],state['inference_graphs'])
                    except ValueError as exc:raise ValueError('maintenance replay conflict: '+str(exc)) from exc
                    state['changes'].append(saved)
                    state['quads']=encode_dataset(ds)
                activity['replayed_activities']=[saved['activity'] for saved in old['changes']]
                state['quads']=encode_dataset(ds)
            invalidate_template(state,old,ds)
            catalog.verify()
            current_validation=validate(state,ds,self.evidence_root)
            if require_valid and current_validation.status!='valid':
                from ..errors import OntologyError
                raise OntologyError('ONTOLOGY_GRAPH_INVALID','contract',current_validation.issues)
            if old.get('quads') or old.get('changes'):
                backup=uuid.uuid4().hex
                write_json(self.store.path(ref)/'rebuilds'/(backup+'.json'),old)
                state.setdefault('rebuild_history',[]).append({'snapshot':backup,'previous_hash':self.binding(old),'replay_changes':replay_changes})
            self.store.save(ref,state)
        return {'validation':current_validation.model_dump(mode='json'),'execution_diagnostics':validation,
                'candidate_hash':self.binding(state),'graph_ref':ref.model_dump(mode='json')}
    def update(self,ref,changes,*,additional_source_hashes=None):
        ref=self.ref(ref);changes=ChangeSet.model_validate(changes) if isinstance(changes,dict) else changes
        if not ref.workspace_id:raise ValueError('update needs a candidate workspace')
        with self.store.lock(ref.graph_id):
            previous=self.state(ref);state=deepcopy(previous)
            from ..capabilities import skill_review
            if additional_source_hashes is not None and not skill_review(state.get('metadata',{})):
                raise ValueError('Additional source registration requires the host workflow policy')
            fingerprint=digest({'changes':changes.model_dump(mode='json'),'source_metadata':additional_source_hashes}) if additional_source_hashes is not None else digest(changes.model_dump(mode='json'))
            existing=next((x for x in state['changes'] if x['change']['idempotency_key']==changes.idempotency_key),None)
            if existing:
                if existing['fingerprint']!=fingerprint:raise ValueError('idempotency key reused with different operation')
                return {'applied':False,'candidate_hash':self.binding(state)}
            if changes.base_revision!=state['base_revision']:raise ValueError('change base revision conflict')
            if changes.expected_hash and changes.expected_hash!=self.binding(state):raise ValueError('candidate hash conflict')
            for e in changes.evidence:
                if e.file and e.file in state['source_hashes'] and file_hash((self.evidence_root/e.file).resolve())!=state['source_hashes'][e.file]:
                    raise ValueError('source snapshot changed; acquire a new immutable evidence file')
            evidence_hashes=check_evidence(changes.evidence,self.evidence_root,state)
            for relative,expected in (additional_source_hashes or {}).items():
                path=(self.evidence_root/relative).resolve()
                if not path.is_relative_to(self.evidence_root) or file_hash(path)!=expected:
                    raise ValueError('Registered source metadata changed')
                if relative in state['source_hashes'] and state['source_hashes'][relative]!=expected:
                    raise ValueError('Registered source metadata may not overwrite an existing batch')
                evidence_hashes[relative]=expected
                state['source_hashes'][relative]=expected
            from .update import apply
            activity=uuid.uuid4().hex
            state['provenance']['activities'].append({'id':activity,'kind':'update','agent_id':changes.agent_id,'run_id':changes.run_id,'reason':changes.reason})
            old_ds=decode_state(previous);ds=apply(state,decode_state(state),changes,activity,state['inference_graphs'])
            for e in changes.evidence:
                if e.file:
                    if file_hash((self.evidence_root/e.file).resolve())!=evidence_hashes[e.file]:raise ValueError('evidence changed during atomic update')
                    state['source_hashes'][e.file]=evidence_hashes[e.file]
            state['quads']=encode_dataset(ds);state['review']=None
            invalidate_template(state,previous,ds)
            from .replay import occurrence_guards
            affected=set(old_ds.quads((None,None,None,None)))^set(ds.quads((None,None,None,None)))
            entities={str(q[i]) for q in affected for i in (0,2) if isinstance(q[i],(URIRef,BNode))}
            entities.update(op.quad.subject.value for op in changes.operations if op.quad)
            for e in changes.evidence:
                entities.update(previous['provenance']['facts'][fid]['quad']['subject']['value'] for fid in e.fact_ids)
            state['changes'].append({'change':changes.model_dump(mode='json'),'fingerprint':fingerprint,'activity':activity,
                'evidence_hashes':evidence_hashes,
                'occurrence_guards':occurrence_guards(previous,entities)})
            for relative,expected in evidence_hashes.items():
                if file_hash((self.evidence_root/relative).resolve())!=expected:raise ValueError('Evidence changed during atomic update')
            self.store.save(ref,state)
            return {'applied':True,'candidate_hash':self.binding(state),'quad_count':len(state['quads']),
                    'template_export_status':state.get('template_export_status')}
    def diff(self,left,right,limit=1000):
        if type(limit) is not int or not 1<=limit<=1000:raise ValueError('diff page limit out of range')
        a=canonical_quads(self.dataset(left));b=canonical_quads(self.dataset(right));added=b-a;removed=a-b
        affected=added|removed
        return GraphDiff(added=[Quad.from_nodes(*q) for q in sorted(added,key=str)[:limit]],removed=[Quad.from_nodes(*q) for q in sorted(removed,key=str)[:limit]],
            affected_entities=sorted({str(t) for q in affected for t in (q[0],q[2]) if isinstance(t,(URIRef,BNode))}),named_graphs=sorted({str(q[3]) for q in affected}),
            added_count=len(added),removed_count=len(removed),truncated=len(added)>limit or len(removed)>limit)
    def validate(self,ref,scope='commit'):return validate(self.state(ref),self.dataset(ref),self.evidence_root,scope)
    def review(self,ref,candidate_hash,review,*,evidence_hashes=None):
        ref=self.ref(ref);review=Review.model_validate(review)
        if not ref.workspace_id:raise ValueError('review needs candidate workspace')
        with self.store.lock(ref.graph_id):
            state=self.state(ref)
            if self.binding(state)!=candidate_hash:raise ValueError('review refers to a changed candidate')
            from ..capabilities import skill_review
            if skill_review(state.get('metadata',{})):
                from ..review_policy import validate_issues
                validate_issues(review)
            elif review.verdict=='approve' and any(i.severity in {'critical','high','medium'} for i in review.issues):raise ValueError('approval contains blocking issues')
            if review.verdict=='approve' and self.validate(ref).status!='valid':raise ValueError('review cannot override deterministic validation')
            state['review']={'candidate_hash':candidate_hash,'result':review.model_dump(mode='json')}
            if evidence_hashes is not None:
                if not skill_review(state.get('metadata',{})):raise ValueError('Supplemental review binding requires the explicit workflow policy')
                for relative,expected in evidence_hashes.items():
                    path=(self.evidence_root/relative).resolve()
                    if not path.is_relative_to(self.evidence_root) or file_hash(path)!=expected:raise ValueError('Reviewer evidence changed')
                state['review']['evidence_hashes']=evidence_hashes
            self.store.save(ref,state)
    def commit(self,ref,*,expected_base,idempotency_key=None):
        ref=self.ref(ref)
        if not ref.workspace_id:raise ValueError('commit needs candidate workspace')
        with self.store.lock(ref.graph_id) as root:
            state=self.state(ref);binding=self.binding(state);key=idempotency_key or binding
            if state['base_revision']!=expected_base:return CommitResult(status='conflict',issues=[{'message':'candidate base differs from expected parent','candidate_base':state['base_revision'],'expected_base':expected_base}])
            completed=state['commits'].get(key)
            if completed:
                if completed['candidate_hash']!=binding:raise ValueError('commit key reused for different candidate')
                return CommitResult.model_validate(completed)
            head=self.store.head(ref.graph_id)
            if state['base_revision']!=expected_base:return CommitResult(status='conflict',issues=[{'message':'candidate base differs from expected parent','head':head,'candidate_base':state['base_revision'],'expected_base':expected_base}])
            if head!=expected_base:
                cursor=head
                while cursor:
                    published_ref=GraphRef(graph_id=ref.graph_id,revision_id=cursor)
                    published_path=self.store.path(published_ref)
                    m=json.loads((published_path/'manifest.json').read_text())
                    if m.get('commit_key')==key and m.get('workspace_id')==ref.workspace_id:
                        if m['candidate_hash']!=binding:raise ValueError('commit key reused for different candidate')
                        self.store.load(published_ref)
                        result=CommitResult(status='completed',graph_ref=published_ref,revision_id=cursor,candidate_hash=binding,manifest_path=str(published_path/'manifest.json'),validation=self.validate(ref),review=state.get('review'))
                        state['commits'][key]=result.model_dump(mode='json');self.store.save(ref,state);return result
                    cursor=m.get('parent_revision')
                return CommitResult(status='conflict',issues=[{'message':'expected parent differs from current head','head':head}])
            report=self.validate(ref)
            if report.status!='valid':return CommitResult(status='invalid',candidate_hash=binding,validation=report)
            review=state.get('review')
            if state['review_policy']=='required':
                if not review or review['candidate_hash']!=binding:return CommitResult(status='pending_review',candidate_hash=binding,validation=report)
                if review['result']['verdict']!='approve':return CommitResult(status='rejected',candidate_hash=binding,validation=report,review=review)
            revisions=root/'revisions';revisions.mkdir(exist_ok=True)
            # Recover a completed bundle whose process crashed before publishing head.
            existing=None
            for p in revisions.glob('r-*'):
                m=json.loads((p/'manifest.json').read_text())
                if m.get('commit_key')==key and m.get('workspace_id')==ref.workspace_id and m.get('candidate_hash')==binding:existing=p;break
            revision=existing.name if existing else 'r-'+uuid.uuid4().hex
            dest=revisions/revision
            if not existing:
                staging=revisions/('.staging-'+revision);staging.mkdir()
                ds=decode_state(state);ds.serialize(destination=staging/'graph.nq',format='nquads')
                union=Graph()
                for q in ds.quads((None,None,None,None)):union.add(q[:3])
                union.serialize(destination=staging/'graph.nt',format='nt');union.serialize(destination=staging/'graph.rdf',format='xml')
                for name,value in [('state',state),('contract',state.get('contract')),('validation',report.model_dump(mode='json')),
                    ('review',review or {'policy':'deterministic','candidate_hash':binding}),('provenance',{**state['provenance'],'graph_id':ref.graph_id,'revision_id':revision,'parent_revision':expected_base}),('changes',state['changes']),('projection_lineage',state.get('projection_lineage',{}))]:write_json(staging/(name+'.json'),value)
                if state.get('contract'):shapes(OntologyContract.model_validate(state['contract'])).serialize(destination=staging/'constraints.ttl',format='turtle')
                if state.get('template'):write_json(staging/'template.json',state['template'])
                manifest={'schemaVersion':'dataelf.ontology.graph.manifest.v2','status':'completed','graph_id':ref.graph_id,'revision_id':revision,
                    'parent_revision':expected_base,'workspace_id':ref.workspace_id,'candidate_hash':binding,'content_hash':content_hash(ds),
                    'contract_hash':digest(state.get('contract')),'constraint_hash':digest({k:(state.get('contract') or {}).get(k) for k in ('classes','properties','constraints','queries')}),
                    'sourceFiles':state['source_hashes'],'provenance_precision':state['provenance_precision'],'commit_key':key,
                    'template_export_status':state.get('template_export_status'),
                    'exports':{'graph.nq':'authoritative named dataset','graph.nt':'union projection','graph.rdf':'union projection'},
                    **state.get('metadata',{}),'artifacts':{p.name:file_hash(p) for p in staging.iterdir() if p.is_file()}}
                write_json(staging/'manifest.json',manifest)
                reread=parse_nquads(staging/'graph.nq')
                if content_hash(reread)!=manifest['content_hash']:raise ValueError('RDF export failed semantic round-trip')
                import os
                for artifact in staging.iterdir():
                    with artifact.open('rb') as handle:os.fsync(handle.fileno())
                staging.rename(dest)
                fd=os.open(revisions,os.O_RDONLY)
                try:os.fsync(fd)
                finally:os.close(fd)
            published=GraphRef(graph_id=ref.graph_id,revision_id=revision)
            self.store.load(published) # Verify every artifact before advancing discovery head.
            write_json(root/'head.json',{'revision_id':revision})
            result=CommitResult(status='completed',graph_ref=published,revision_id=revision,candidate_hash=binding,
                                manifest_path=str(dest/'manifest.json'),validation=report,review=review)
            state['commits'][key]=result.model_dump(mode='json');self.store.save(ref,state)
            return result
    def import_rdf(self,path,*,graph_id=None,manifest_path=None,review_policy='deterministic',format=None,default_graph='urn:dataelf:graph:import'):
        path=Path(path).resolve()
        if not path.is_relative_to(self.evidence_root):raise ValueError('import must be inside evidence workspace')
        ds=Dataset(default_union=True)
        fmt=format or {'.nq':'nquads','.nt':'nt','.rdf':'xml','.ttl':'turtle','.trig':'trig'}.get(path.suffix)
        if fmt=='nquads':ds=parse_nquads(path)
        elif fmt=='trig':ds.parse(path,format=fmt)
        elif fmt in {'nt','xml','turtle'}:ds.graph(URIRef(default_graph)).parse(path,format=fmt)
        else:raise ValueError('unsupported RDF import format')
        ref=self.workspace('create',graph_id=graph_id,review_policy=review_policy)
        with self.store.lock(ref.graph_id):
            state=self.state(ref)
            quads=canonical_quads(ds)
            persistent=Dataset(default_union=True)
            def persistent_term(term):
                return URIRef('urn:dataelf:blank:'+ref.graph_id+':'+str(term)) if isinstance(term,BNode) else term
            for s,p,o,g in quads:persistent.graph(persistent_term(g)).add(tuple(persistent_term(t) for t in (s,p,o)))
            state['quads']=encode_dataset(persistent);state['provenance_precision']='limited'
            state['blank_node_strategy']='canonicalize on import, then skolemize under the logical graph ID; stable across revisions of this graph'
            state['source_hashes'][str(path.relative_to(self.evidence_root))]=file_hash(path)
            state['import']={'file':str(path.relative_to(self.evidence_root)),'hash':file_hash(path),'note':'legacy/imported facts lack exact derivation evidence'}
            if manifest_path:
                manifest_path=Path(manifest_path).resolve()
                if not manifest_path.is_relative_to(self.evidence_root):raise ValueError('manifest outside evidence workspace')
                manifest=json.loads(manifest_path.read_text());folder=manifest_path.parent
                if manifest.get('schemaVersion') not in {'dataelf.ontology.manifest.v1','dataelf.ontology.graph.manifest.v2'} or manifest.get('status')!='completed':raise ValueError('unsupported/incomplete import manifest')
                verified=set()
                for rel,expected in manifest.get('artifacts',{}).items():
                    target=(folder/rel).resolve()
                    if not target.is_file():target=(folder.parent/rel).resolve()
                    if not target.is_relative_to(self.evidence_root) or file_hash(target)!=expected:raise ValueError('import manifest artifact mismatch')
                    verified.add(target)
                if path not in verified:raise ValueError('RDF import is not bound to this manifest')
                contract_path=folder/'contract.json'
                if not contract_path.exists():contract_path=folder.parent/'stage1/contract.json'
                if contract_path.exists():
                    state['contract']=OntologyContract.model_validate_json(contract_path.read_text()).model_dump(mode='json')
                    state['graph_names']={k:str(iri(state['contract']['namespace'],v)) for k,v in state['contract']['graphs'].items()}
                state['source_hashes'].update(manifest.get('sourceFiles',{}))
                native_state_path=folder/'state.json'
                if native_state_path in verified:
                    native=json.loads(native_state_path.read_text())
                    for field in ('source_inputs','graph_names','inference_graphs','changes','projection_lineage'):
                        if field in native:state[field]=native[field]
                if (folder/'provenance.json').exists():state['provenance']=json.loads((folder/'provenance.json').read_text());state['provenance_precision']=manifest.get('provenance_precision','limited')
                state['import']['manifest']=manifest
            self.store.save(ref,state)
        return ref
