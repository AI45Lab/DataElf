from __future__ import annotations
import time
import asyncio
import httpx
from dataelf_server.app import create_app
from dataelf_server.jobs.manager import JobManager
from dataelf_server.workflows.pipeline import ServerPipeline
from tests.server.test_workflow import components


def test_http_uses_shared_workflow_and_async_intent_errors(tmp_path):
    cfg,profile,explorer,_ = components(tmp_path)
    pipeline=ServerPipeline(cfg,profile_factory=lambda settings:profile,explorer_factory=lambda settings:explorer)
    app=create_app(settings=cfg,manager_factory=lambda settings:JobManager(settings,pipeline=pipeline),validate_runtime=False)
    async def terminal(client,job_id):
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            state=(await client.get(f'/api/v1/insight/jobs/{job_id}')).json()['data']
            if state['status'] in {'completed','failed'}:
                return state
            await asyncio.sleep(.01)
        raise AssertionError('HTTP task did not finish')
    async def scenario():
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
            accepted=await client.post('/api/v1/insight/jobs',json={'query':'2026-08-27 快讯模块总结'})
            assert accepted.status_code==202
            job_id=accepted.json()['data']['job_id']
            assert (await terminal(client,job_id))['status']=='completed'
            result=await client.get(f'/api/v1/insight/jobs/{job_id}/result')
            assert result.status_code==200
            public=result.json()['data']['insights'][0]
            assert set(public)=={'insight_id','title','content','sources'}
            assert public['sources']==[{'title':'模型工具发布','url':'https://example.com/news'}]
            invalid=await client.post('/api/v1/insight/jobs',json={'query':'不支持的指令'})
            assert invalid.status_code==202
            failed_id=invalid.json()['data']['job_id']
            state=await terminal(client,failed_id)
            assert state['error']['category']=='intent_error'
            assert (await client.get(f'/api/v1/insight/jobs/{failed_id}/result')).status_code==422
            retry=await client.post(f'/api/v1/insight/jobs/{failed_id}/retry')
            assert retry.status_code==202
            assert retry.json()['trace_id']!=invalid.json()['trace_id']
            assert (await terminal(client,failed_id))['status']=='failed'
            assert len(app.state.manager.store.list_attempts(failed_id))==2
            assert (await client.post(f'/api/v1/insight/jobs/{job_id}/retry')).status_code==409
    asyncio.run(scenario())
