"""采集结束通知仅做定向验证，不打开浏览器或执行真实采集。"""
from datetime import date, datetime, timezone
from types import SimpleNamespace
from uuid import uuid4
import json
from unittest.mock import patch
import httpx
from compass_collector.collection_completion import notify_collection_finished, send_completion_file


def test_manifest_is_immutable_and_network_failure_keeps_terminal_state(tmp_path, monkeypatch):
    """同一次执行保留首个载荷，网络失败不改变任务状态。"""
    monkeypatch.setenv('RANKING_API_URL','https://api.example')
    monkeypatch.setenv('RANKING_SYNC_TOKEN','test-only')
    task=SimpleNamespace(id='daily',platform='compass')
    result=SimpleNamespace(status=SimpleNamespace(value='partial_success'))
    candidate=SimpleNamespace(task=task,batch_id='a'*32)
    execution=uuid4().hex
    moment=datetime(2026,10,4,18,tzinfo=timezone.utc)
    with patch('compass_collector.collection_completion.send_completion_file',side_effect=RuntimeError('test failure')):
        notify_collection_finished(execution,date(2026,10,4),moment,[task],{'daily':result},[candidate],tmp_path)
        path=tmp_path/'collection-completions'/f'{execution}.json'
        original=path.read_text()
        notify_collection_finished(execution,date(2026,10,4),datetime.now(timezone.utc),[task],{'daily':result},[],tmp_path)
    assert path.read_text()==original
    assert json.loads(original)['tasks'][0]['batch_id']=='a'*32
    assert result.status.value=='partial_success'


def test_receipt_retries_same_execution_without_secret_in_file(tmp_path, monkeypatch):
    """服务凭据仅进入请求头，成功回执单独保存。"""
    monkeypatch.setenv('RANKING_API_URL','https://api.example')
    monkeypatch.setenv('RANKING_SYNC_TOKEN','test-only')
    path=tmp_path/'notification.json'
    payload={'execution_id':str(uuid4()),'tasks':[]}
    path.write_text(json.dumps(payload))
    with patch('httpx.Client.post',return_value=httpx.Response(200,json={'accepted':True},request=httpx.Request('POST','https://api.example'))) as post:
        assert send_completion_file(path)=={'accepted':True}
    assert post.call_args.kwargs['json']==payload
    assert post.call_args.kwargs['headers']['Authorization']=='Bearer test-only'
    assert 'test-only' not in path.read_text()
    assert path.with_suffix('.receipt.json').exists()


def test_notification_uses_ranking_date_across_midnight(tmp_path, monkeypatch):
    """命令次日结束时仍按计划榜单日期通知，不把旧数据当作当天输入。"""
    monkeypatch.setenv('RANKING_API_URL','https://api.example')
    monkeypatch.setenv('RANKING_SYNC_TOKEN','test-only')
    task=SimpleNamespace(id='daily',platform='taobao')
    result=SimpleNamespace(status=SimpleNamespace(value='failed'))
    with patch('compass_collector.collection_completion.send_completion_file'):
        notify_collection_finished(uuid4().hex,date(2026,10,5),datetime.now(timezone.utc),[task],{'daily':result},[],tmp_path,task_dates={'daily':date(2026,10,4)})
    files=list((tmp_path/'collection-completions').glob('*.json'))
    assert len(files)==1
    assert json.loads(files[0].read_text())['business_date']=='2026-10-04'


def test_outbox_continues_after_failure_and_skips_receipts(tmp_path):
    """一条网络失败不阻塞后续通知，已有回执不重复调用。"""
    from compass_collector.collection_completion import retry_pending_completions
    # Synthetic manifests avoid real collection or HTTP calls.
    directory=tmp_path/'collection-completions'
    directory.mkdir()
    paths=[directory/f'{index}.json' for index in range(3)]
    for path in paths:
        path.write_text('{}')
    paths[2].with_suffix('.receipt.json').write_text('{}')
    with patch('compass_collector.collection_completion.send_completion_file',side_effect=[RuntimeError('network'),{'accepted':True}]) as send:
        result=retry_pending_completions(tmp_path,limit=3)
    assert result=={'accepted':1,'failed':1}
    assert [call.args[0] for call in send.call_args_list]==paths[:2]
