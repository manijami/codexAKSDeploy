#!/usr/bin/env python3
"""Verify installed SDK request serialization with synthetic data and no network."""
import asyncio
import importlib.metadata
import json
import os
os.environ['RAGAS_DO_NOT_TRACK'] = 'true'
import httpx
from openai import AsyncOpenAI
from pydantic import BaseModel
from ragas.llms import llm_factory

class Output(BaseModel):
    score: float

async def main():
    requests = []
    async def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'id': 'synthetic', 'object': 'chat.completion',
            'created': 0, 'model': 'synthetic', 'choices': [{'index': 0, 'finish_reason': 'stop',
            'message': {'role': 'assistant', 'content': '{"score": 1.0}'}}],
            'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = AsyncOpenAI(api_key='SYNTHETIC', base_url='https://synthetic.invalid/v1', http_client=http, max_retries=0)
        llm = llm_factory('nvidia/nemotron-3.5-super-vl-120b-a12b', client=client,
                          max_tokens=4096, max_retries=1,
                          extra_body={'chat_template_kwargs': {'enable_thinking': False}})
        answer = await llm.agenerate('Synthetic offline serialization check', Output)
        assert answer.score == 1.0 and len(requests) == 1
        body = requests[0]
        assert body['max_tokens'] == 4096
        assert body['chat_template_kwargs'] == {'enable_thinking': False}
        assert 'max_completion_tokens' not in body
        print(json.dumps({'offline_mock_serialization': 'PASS', 'network_calls': 0,
            'packages': {n: importlib.metadata.version(n) for n in ('ragas','openai','instructor','httpx')},
            'wire_parameters': {k: body[k] for k in ('max_tokens','chat_template_kwargs','temperature','top_p')}}))

if __name__ == '__main__':
    asyncio.run(main())
