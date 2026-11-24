import json
from pathlib import Path
import pytest
from dataelf.ontology.capabilities import CLI_SKILL_REVIEW
from dataelf.ontology.contracts import OntologyRequest, OntologyContract
from dataelf.ontology.config import OntologyConfig
from dataelf.ontology.pipeline import run_ontology
from dataelf.ontology.task_sources import task_catalog
from dataelf.ontology.sources import write_json, digest
from dataelf.ontology.tools import dispatch
from dataelf.ontology.graph import GraphService
from dataelf.domains.ai_index.evidence import EvidenceCatalog


def query_contract(row):
    return OntologyContract.model_validate({'domain':'ai_index','namespace':'urn:sample:',
        'classes':{'Entry':{}},'properties':{'value':{'domain':'Entry','range':'xsd:integer'}},
        'mappings':[{'id':'entries','source':row['source_id'],'evidence':[row['response_file']+'#/0'],
            'nodes':[{'id':'entry','class_id':'Entry','iri':{'op':'concat','args':['urn:sample:entry/',{'path':'/id'}]}}],
            'triples':[{'subject':'entry','predicate':'value','value':{'path':'/value'}}]}]})


def register(tmp_path, name='input.json'):
    (tmp_path/name).write_text('[{"id":"a","value":0}]')
    return EvidenceCatalog(tmp_path).import_file(name,role='ontology_modeler')


def test_zero_source_only_allowed_by_explicit_host_policy(tmp_path):
    with pytest.raises(ValueError,match='at least one'):
        OntologyRequest(domain='ai_index',workspace=tmp_path,sources=[])
    request=OntologyRequest(domain='ai_index',workspace=tmp_path,workflow_policy=CLI_SKILL_REVIEW)
    _,catalog=task_catalog(request)
    assert not catalog.records
    with pytest.raises(ValueError,match='Unregistered workflow policy'):
        OntologyRequest(domain='retail',workspace=tmp_path,workflow_policy='unknown')


def test_append_catalog_exact_dependencies_and_pipeline_publication(tmp_path):
    request=OntologyRequest(domain='ai_index',workspace=tmp_path,workflow_policy=CLI_SKILL_REVIEW)
    modeler_calls=[]
    def agent(role,context,directory,limits):
        assert context['execution_policy']=='unbounded' and limits.max_requests is None
        if role=='modeler':
            assert context['request']['sources']==[]
            modeler_calls.append(context)
            row=register(tmp_path)
            return query_contract(row).model_dump(mode='json')
        bound=context['candidate_hash']
        # An unrelated supplemental source must not change this candidate.
        register(tmp_path,'extra.json')
        service=GraphService(context['graph_root'],tmp_path)
        assert service.binding(service.state(context['graph_ref']))==bound
        assert len(context['request']['sources'])==1
        return {'verdict':'approve','summary':'Independently checked source and integer zero','issues':[]}
    result=run_ontology(request,config=OntologyConfig(save_as_template=True),agent=agent)
    assert result.status=='completed',result.error_message
    assert len(modeler_calls)==1 and result.template_path is None
    service=GraphService(Path(result.bundle_path).parents[2],tmp_path)
    state=service.state(result.graph_ref)
    assert len(state['source_inputs'])==1 and len(EvidenceCatalog(tmp_path).sources())==2
    source=next(iter(state['source_hashes']))
    (tmp_path/source).write_text('[]')
    assert service.validate(result.graph_ref).status=='invalid'


def test_saved_reads_survive_append_but_reject_bound_tamper(tmp_path):
    from dataelf.ontology.read_cache import save_read,read
    row=register(tmp_path)
    request=OntologyRequest(domain='ai_index',workspace=tmp_path,workflow_policy=CLI_SKILL_REVIEW)
    context={'request':request.model_dump(mode='json'),'role':'modeler','source_fingerprint':'initial',
             'read_cache_directory':str(tmp_path/'cache')}
    path=tmp_path/'context.json';write_json(path,context)
    saved=save_read(path,context,'ontology_evidence',{'operation':'replay','file':row['response_file']},{'value':0})
    register(tmp_path,'unused.json')
    args={'result_id':saved['cached_read']['result_id'],'offset':0,'max_bytes':8000}
    assert json.loads(read(path,context,args)['text'])=={'value':0}
    (tmp_path/row['response_file']).write_text('[]')
    with pytest.raises(ValueError,match='dependency changed'):read(path,context,args)


def test_modeling_stage_propagates_published_ref_and_scalar_metrics(tmp_path,monkeypatch):
    from dataelf.discovery.contracts import DiscoveryContext,DiscoveryJob,DomainManifest,JobSpec
    from dataelf.domains.ai_index.modeler import AIIndexModeler
    spec=JobSpec(domain='ai_index',objective='Inspect sourced entries',workflow_policy=CLI_SKILL_REVIEW)
    job=DiscoveryJob(job_id='job_stage',spec=spec,workspace_path=str(tmp_path))
    context=DiscoveryContext(workspace_path=str(tmp_path),spec=spec,
        manifest=DomainManifest(domain='ai_index',version='1',display_name='AI',plugin='test'))
    def agent(role,ctx,directory,limits):
        if role=='modeler':return query_contract(register(tmp_path)).model_dump(mode='json')
        return {'verdict':'approve','issues':[],'summary':'Verified entry and value'}
    monkeypatch.setattr('dataelf.ontology.runtime.PiOntologyAgent',lambda *a,**k:agent)
    modeler=AIIndexModeler.__new__(AIIndexModeler)
    modeler.ontology_config=OntologyConfig();modeler.runtime_config=None
    result=modeler.run(job,context)
    assert result.status=='completed',result.error_message
    assert result.context['graph_ref']['revision_id'] and result.context['graph_root']
    assert 'graph_ref' not in result.model_fields
    assert all(isinstance(v,(str,int,float,bool)) for v in result.metrics.values())


def test_query_metadata_and_supplemental_review_evidence_are_frozen(tmp_path):
    row=register(tmp_path);service=GraphService(tmp_path/'graphs',tmp_path)
    ref=service.workspace('create',graph_id='checked',review_policy='required')
    state=service.state(ref);state.setdefault('metadata',{})['workflow_policy']=CLI_SKILL_REVIEW;service.store.save(ref,state)
    service.materialize(ref,query_contract(row),EvidenceCatalog(tmp_path).sources())
    from tests.test_ai_index_collaboration import issue
    binding=service.binding(service.state(ref))
    with pytest.raises(ValueError,match='blocking'):
        service.review(ref,binding,{'verdict':'approve','summary':'Cannot ignore low confirmed errors','issues':[issue()]})
    extra=tmp_path/'review.json';extra.write_text('[{"checked":true}]')
    EvidenceCatalog(tmp_path).import_file('review.json',role='ontology_reviewer')
    hashes=EvidenceCatalog(tmp_path).reviewer_dependencies('graph')
    service.review(ref,binding,{'verdict':'approve','summary':'Checked exact source and zero','issues':[]},evidence_hashes=hashes)
    register(tmp_path,'unused.json')
    assert service.validate(ref).status=='valid'
    # A reviewer supplement is frozen independently of graph-generating inputs.
    (tmp_path/next(k for k in hashes if not k.endswith('record.json'))).write_text('[]')
    assert service.validate(ref).status=='invalid'
    (tmp_path/next(k for k in hashes if not k.endswith('record.json'))).write_text('[{"checked":true}]')
    assert service.validate(ref).status=='valid'
    metadata=EvidenceCatalog(tmp_path).root/row['query_id']/'record.json'
    changed=json.loads(metadata.read_text());changed['params']={'unrecorded_change':True};write_json(metadata,changed)
    assert service.validate(ref).status=='invalid'


@pytest.mark.parametrize('suffix,content,count',[('jsonl','{"id":"a","value":0}\n',1),('csv','id,value\na,0\n',1),('csv','id,value\n',0)])
def test_local_registered_formats_preserve_complete_bytes(tmp_path,suffix,content,count):
    path=tmp_path/('import.'+suffix);path.write_text(content)
    catalog=EvidenceCatalog(tmp_path);record=catalog.import_file(path.name,role='ontology_modeler')
    assert record['record_count']==count and record['format']==suffix
    assert (tmp_path/record['response_file']).read_bytes()==path.read_bytes()
    assert catalog.sources()[0].format==suffix
