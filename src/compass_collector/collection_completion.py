"""采集结束通知与榜单同步独立；失败保留同一幂等载荷，不影响采集终态。"""
import json
import logging
import os
from pathlib import Path
from uuid import UUID, uuid5
import httpx


def send_completion_file(path: Path) -> dict | None:
    """重试已有结束通知，不生成新执行身份，也不重新采集商品。"""
    # 与榜单同步共用受限服务凭证，不写入快照或日志。
    base, token = os.getenv('RANKING_API_URL', ''), os.getenv('RANKING_SYNC_TOKEN', '')
    if not base and not token:
        return None
    if not base or not token:
        raise ValueError('collection_completion_config_incomplete')
    # 公开服务器使用 HTTPS，本机隔离验收可使用回环 HTTP。
    parsed = httpx.URL(base)
    if (parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.host in ('127.0.0.1','localhost'))) or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('collection_completion_url_invalid')
    # 载荷只包含日期、任务终态和批次身份，不包含原始请求及认证信息。
    payload = json.loads(path.read_text())
    with httpx.Client(timeout=20, follow_redirects=False) as client:
        response = client.post(base.rstrip('/')+'/api/collection-completions', json=payload,
            headers={'Authorization':'Bearer '+token})
        if response.status_code != 200:
            raise ValueError(f'collection_completion_http_{response.status_code}')
        receipt = response.json()
    path.with_suffix('.receipt.json').write_text(json.dumps(receipt,ensure_ascii=False))
    return receipt



def retry_pending_completions(runtime_root, limit=3, exclude=()):
    """逐条重试尚无回执的通知；失败不阻塞其他通知，自动调用有数量上限。"""
    # Receipt files and the current manifests must not be replayed redundantly.
    excluded = set(exclude)
    pending = [path for path in sorted((Path(runtime_root)/'collection-completions').glob('*.json'))
               if path not in excluded and not path.name.endswith('.receipt.json') and not path.with_suffix('.receipt.json').exists()]
    accepted = failed = 0
    for path in pending[:limit] if limit is not None else pending:
        try:
            if send_completion_file(path) is not None:
                accepted += 1
        except Exception as error:
            failed += 1
            logging.getLogger(__name__).warning('collection_completion_retry_failed: %s',type(error).__name__)
    return {'accepted':accepted,'failed':failed}

def notify_collection_finished(execution_id, business_date, finished_at, tasks, results, candidates, runtime_root, task_dates=None):
    """所有分类处理及发布尝试结束后调用；保留浏览器手工检查不会阻塞此通知。"""
    if not os.getenv('RANKING_API_URL') and not os.getenv('RANKING_SYNC_TOKEN'):
        return
    # 已正式发布候选提供真实批次引用，同步是否完成由服务端独立核验。
    published = {row.task.id:row.batch_id for row in candidates}
    # Planned ranking dates may differ from CLI start date for midnight runs or explicit historical tasks.
    dates = dict(task_dates or {})
    for candidate in candidates:
        batch = getattr(candidate,'collected_batch',None)
        if batch is not None:
            dates[candidate.task.id] = batch.business_date
    task_groups = {}
    for task in tasks:
        status = results[task.id].status.value
        # 认证失败和错过执行没有成功数据；尚未开始与中断阻止自动触发。
        if status in ('auth_required','missed'):
            status = 'failed'
        elif status == 'skipped_busy':
            status = 'not_started'
        if status in ('success','partial_success') and task.id not in published:
            status = 'failed'
        task_groups.setdefault(dates.get(task.id,business_date),[]).append({'platform':task.platform,'task_id':task.id,'status':status,'batch_id':published.get(task.id)})
    # Keep the current execution out of the subsequent historical outbox retry pass.
    current_paths = []
    for day, task_rows in task_groups.items():
        # A deterministic per-date identity preserves retries for executions spanning multiple ranking dates.
        manifest_id=UUID(execution_id) if day==business_date else uuid5(UUID(execution_id),'completion:'+day.isoformat())
        payload = {'execution_id':str(manifest_id), 'business_date':day.isoformat(),
                   'finished_at':finished_at.isoformat(),'tasks':task_rows}
        path = Path(runtime_root)/'collection-completions'/f'{manifest_id.hex}.json'
        current_paths.append(path)
        try:
            path.parent.mkdir(parents=True,exist_ok=True)
            # A replay keeps the first manifest, including its original completion timestamp.
            if not path.exists():
                path.write_text(json.dumps(payload,ensure_ascii=False))
            send_completion_file(path)
        except Exception as error:
            # 不输出 HTTP 头、环境或异常正文；采集业务状态保持原样。
            logging.getLogger(__name__).warning('collection_completion_failed: %s; retry scripts/retry_collection_completions.py',type(error).__name__)

    retry_pending_completions(runtime_root, limit=3, exclude=current_paths)
