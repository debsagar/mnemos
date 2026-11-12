"""Offline check of the packaged stdio MCP entry point."""
import json
import os
import subprocess
import sys
import tempfile

with tempfile.TemporaryDirectory(prefix='mnemos-mcp-test-') as home:
    requests = [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
        {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
         'params': {'name': 'stats', 'arguments': {}}},
    ]
    result = subprocess.run(
        [sys.executable, '-m', 'mnemos.mcp_server'],
        input=''.join(json.dumps(r) + '\n' for r in requests),
        text=True, capture_output=True, check=True, timeout=20,
        env={**os.environ, 'MNEMOS_HOME': home})
    responses = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(responses) == 3
    assert responses[0]['result']['serverInfo']['name'] == 'mnemos'
    assert {t['name'] for t in responses[1]['result']['tools']} == {
        'remember', 'recall', 'why', 'due', 'stats'}
    stats = json.loads(responses[2]['result']['content'][0]['text'])
    assert stats['turns'] == 0
print('MCP initialize, tool listing and stats: ok')
