"""只重试已持久化的结束通知，不运行 RPA、不重建业务批次。"""
import argparse
from pathlib import Path
from dotenv import load_dotenv
from compass_collector.collection_completion import retry_pending_completions


def main():
    """环境文件显式指定，默认遍历采集工程的结束通知目录。"""
    parser=argparse.ArgumentParser()
    parser.add_argument('--runtime-root',type=Path,default=Path('runtime'))
    parser.add_argument('--env-file',type=Path)
    args=parser.parse_args()
    if args.env_file:
        load_dotenv(args.env_file)
    # Explicit retries process the entire outbox, while normal collection uses a bounded pass.
    result=retry_pending_completions(args.runtime_root,limit=None)
    print('accepted',result['accepted'],'failed',result['failed'])
    if result['failed']:
        raise SystemExit(1)


if __name__=='__main__':
    main()
