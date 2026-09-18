import copy
import json
import os
from pathlib import Path

import pytest

from common import atomic_json, check_drift, resolve, RuntimeUnavailable
from studio import profiles, runner
from studio.diagnostics import output_diagnostics, exception_message
from studio.repository import snapshot_task, collect_trials


def test_task_snapshot_excludes_oracle_logs_and_checks_all_inputs(tmp_path):
    source = tmp_path / 'task'
    source.mkdir()
    (source / 'task.toml').write_text('[environment]\n')
    (source / 'instruction.md').write_text('Repair the function')
    for d in ('tests', 'environment', 'solution'):
        (source / d).mkdir()
    (source / 'tests/test.sh').write_text('#!/bin/sh\nexit 0\n')
    (source / 'oracle-logs').mkdir()
    secret = source / 'oracle-logs/private.log'
    secret.write_text('not an input')
    secret.chmod(0)
    try:
        dest = tmp_path / 'snapshot'
        hashes = snapshot_task(source, dest)
        assert 'tests/test.sh' in hashes
        assert not (dest / 'oracle-logs').exists()
        assert os.access(dest / 'tests/test.sh', os.X_OK)
        (source / 'tests/test.sh').unlink()
        (source / 'tests/test.sh').symlink_to(secret)
        with pytest.raises(RuntimeError, match='symlink'):
            snapshot_task(source, tmp_path / 'bad')
    finally:
        secret.chmod(0o600)


def test_partial_repository_evidence_has_no_score(tmp_path):
    expected = [{'id': 'a', 'language': 'python'}, {'id': 'b', 'language': 'typescript'}]
    atomic_json(tmp_path / 'harbor/job/trial/result.json', {'task_name': 'bench-studio/a', 'verifier_result': {'rewards': {'reward': 1}}})
    r = collect_trials(tmp_path, expected, job_error='PermissionError: unreadable setup log')
    assert r['score'] is None and r['passed'] == 1 and r['completed_count'] == 1
    assert r['tasks'][1]['status'] == 'not_completed'
    assert r['metrics'][0]['rate'] is None
    atomic_json(tmp_path / 'harbor/job/trial-b/result.json', {'task_name': 'bench-studio/b', 'verifier_result': {'rewards': {'reward': 0}}})
    assert collect_trials(tmp_path, expected)['score'] == 50


@pytest.mark.parametrize('reward', [None, 0.5, '1'])
def test_missing_or_malformed_reward_is_infrastructure(tmp_path, reward):
    atomic_json(tmp_path / 'harbor/job/trial/result.json', {'task_name': 'bench-studio/a', 'verifier_result': {'rewards': {'reward': reward}}})
    r = collect_trials(tmp_path, [{'id':'a','language':'python'}])
    assert r['score'] is None and r['tasks'][0]['status'] == 'infrastructure_error'


def test_exception_groups_expose_cause():
    error = ExceptionGroup('unhandled errors in a TaskGroup', [PermissionError('oracle log unreadable')])
    assert exception_message(error) == 'PermissionError: oracle log unreadable'


def test_output_exhaustion_and_repetition_are_separate_from_test_errors():
    d = output_diagnostics({'finish_reason':'length', 'response':'', 'reasoning':'thinking ' + '0' * 2000})
    assert d['failure_kind'] == 'output_exhausted'
    assert d['repetition_detected'] and d['answer_chars'] == 0
    assert not output_diagnostics({'finish_reason':'stop','response':'return 1','reasoning':'A short thought'})['repetition_detected']


def test_profile_versions_and_reasoning_budgets():
    p = next(x for x in profiles.builtins() if x['id'] == 'coding-checks')
    assert p['version'] == 2 and p['parameters']['reasoning_budget_tokens'] is None
    p['size'] = 'full'
    profiles.attach_manifest(p)
    assert 'HumanEval_92_any_int' in p['dataset_versions']['exclusions']['typescript']


def healthy_snapshot():
    return {'models': {'data': [{'id':'daytime','x_ollama_router': {'complete': True, 'health':{'available': True},'context_window':10000,'upstream_model':'qwen'}}]},
            'runtime': {'services':[{'model':'qwen','id':'container','image_id':'image','healthy':True,'running':True,'started_at':'start','restart_count':0}]}}


def test_transient_health_check_does_not_kill_active_run(monkeypatch):
    snap = healthy_snapshot()
    m = {'requested_targets':['daytime'],'resolved':{'daytime':resolve(snap,'daytime')},'mode':'sequential'}
    snap['runtime']['services'][0]['healthy'] = False
    monkeypatch.setattr(runner, 'snapshot', lambda _: snap)
    monkeypatch.setattr(runner, 'record', lambda _: None)
    assert runner.check_current(m) is None
    assert 'health_warning' in m
    m['health_unavailable_since'] -= 91
    with pytest.raises(RuntimeUnavailable, match='90s'):
        runner.check_current(m)
    snap['runtime']['services'][0]['healthy'] = True
    runner.check_current(m)
    assert 'health_warning' not in m


def test_unhealthy_service_does_not_hide_identity_drift(monkeypatch):
    snap = healthy_snapshot()
    m = {'requested_targets':['daytime'],'resolved':{'daytime':copy.deepcopy(resolve(snap,'daytime'))},'mode':'sequential'}
    snap['runtime']['services'][0].update(healthy=False,id='replacement')
    monkeypatch.setattr(runner, 'snapshot', lambda _: snap)
    with pytest.raises(RuntimeError, match='changed during'):
        runner.check_current(m)


def test_same_container_restart_is_drift():
    snap = healthy_snapshot()
    before = copy.deepcopy(resolve(snap,'daytime'))
    snap['runtime']['services'][0]['restart_count'] = 1
    with pytest.raises(RuntimeError, match='changed during'):
        check_drift(before, resolve(snap,'daytime'))


@pytest.mark.parametrize('source,tests,passes', [
    ('export function double(n: number) { return n * 2; }', "assert.equal(double(4), 8);", True),
    ("import * as crypto from 'crypto'; export function md5(s: string) { return crypto.createHash('md5').update(s).digest('hex'); }", "assert.equal(md5('hello'), '5d41402abc4b2a76b9719d911017c592');", True),
    ('function double(n: number) { return n; }', 'assert.equal(double(4), 8);', False),
])
def test_typescript_compiler_with_node_imports_and_exports(monkeypatch, source, tests, passes):
    # Handwritten regression fixtures, not model-produced programs.
    from studio.typescript import evaluate
    deps = Path(__file__).resolve().parents[1] / 'verifier/node_modules'
    if not deps.exists():
        pytest.skip('Run npm ci --prefix verifier for compiler integration tests')
    monkeypatch.setenv('BENCH_TSC',str(deps / '.bin/tsc'))
    monkeypatch.setenv('BENCH_TYPE_ROOTS',str(deps / '@types'))
    result = evaluate(source, "declare var require: any;\nconst assert = require('node:assert');\n" + tests)
    assert result['passed'] is passes, result


def test_bounded_thinking_validation(monkeypatch):
    builtin = next(x for x in profiles.builtins() if x['id'] == 'coding-checks')
    monkeypatch.setattr(profiles, 'get', lambda _: copy.deepcopy(builtin))
    good = profiles.configure('coding-checks','quick',{'reasoning_budget_tokens':6144,'reasoning_effort':'xhigh'})
    assert good['parameters']['max_tokens'] == 8192
    for value in [8192, 9000, -1, 1.5, True]:
        with pytest.raises(ValueError):
            profiles.configure('coding-checks','quick',{'reasoning_budget_tokens':value})
    with pytest.raises(ValueError):
        profiles.configure('coding-checks','quick',{'reasoning_budget_tokens':100,'reasoning_effort':'off'})


def test_task_exclusion_is_applied_before_selection(tmp_path, monkeypatch):
    from studio import quality_worker
    monkeypatch.setattr(quality_worker,'ROOT',tmp_path)
    atomic_json(tmp_path / 'typescript.json', [{'name':name,'prompt':'function f() {','tests':''} for name in ['a','b','c','d','HumanEval_92_any_int']])
    spec = {'languages':['typescript'],'size':'full','dataset_versions':{'exclusions':{'typescript':{'HumanEval_92_any_int':'invalid'}}}}
    assert len(quality_worker.choose_tasks(spec)) == 4
    assert all(t['id'] != 'HumanEval_92_any_int' for t in quality_worker.choose_tasks(spec))


def test_historical_repository_exception_is_explained_without_mutation(tmp_path):
    log = tmp_path / 'run.log'
    log.write_text('ExceptionGroup: unhandled errors in a TaskGroup\n    | PermissionError: unreadable oracle log\n')
    before = log.read_bytes()
    r = collect_trials(tmp_path / 'daytime',[{'id':'a','language':'python'}], job_error='unhandled errors in a TaskGroup (1 sub-exception)')
    assert r['infrastructure_error'] == 'PermissionError: unreadable oracle log'
    assert log.read_bytes() == before


def test_generation_sends_optional_thinking_limit(monkeypatch):
    import httpx
    from studio import quality_worker
    captured = []
    original = httpx.Client
    def handler(request):
        captured.append(json.loads(request.content))
        packets = [{'choices':[{'delta':{'content':'def f(): pass'},'finish_reason':'stop'}], 'usage':{'prompt_tokens':10,'completion_tokens':5}}]
        text = ''.join('data: '+json.dumps(p)+'\n\n' for p in packets)+'data: [DONE]\n\n'
        return httpx.Response(200,text=text)
    monkeypatch.setattr(quality_worker.httpx, 'Client', lambda **kw: original(transport=httpx.MockTransport(handler),**kw))
    params={'temperature':0,'top_p':1,'seed':42,'max_tokens':8192,'reasoning_effort':'xhigh','reasoning_budget_tokens':6144}
    task={'id':'test','language':'python','prompt':'def f():'}
    quality_worker.generate('http://router/v1','test',task,params,lambda _:None)
    assert captured[0]['reasoning_budget_tokens'] == 6144
    params['reasoning_budget_tokens'] = None
    quality_worker.generate('http://router/v1','test',task,params,lambda _:None)
    assert 'reasoning_budget_tokens' not in captured[1]


def test_readiness_wait_rechecks_identity_before_new_request(monkeypatch):
    import common
    snap = healthy_snapshot()
    expected = copy.deepcopy(resolve(snap,'daytime'))
    bad = copy.deepcopy(snap)
    bad['runtime']['services'][0]['healthy'] = False
    snapshots = iter([bad,snap])
    monkeypatch.setattr(common,'snapshot',lambda _:next(snapshots))
    monkeypatch.setattr('time.sleep',lambda _:None)
    assert common.wait_for_runtime({},'daytime',expected)['canonical'] == 'qwen'
    bad['runtime']['services'][0]['id'] = 'new-container'
    monkeypatch.setattr(common,'snapshot',lambda _:bad)
    with pytest.raises(RuntimeError,match='changed during'):
        common.wait_for_runtime({},'daytime',expected)
