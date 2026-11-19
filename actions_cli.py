import argparse
import json
import sys
from pathlib import Path
from board_actions import ActionStore
from platform_support import IS_WINDOWS
from runtime_paths import DATA_DIR


def main(argv=None,store=None):
    parser=argparse.ArgumentParser(prog='wb actions',description='Huginn 연결 설정과 스킬 바로가기 등록. 스킬을 실행하지 않습니다.')
    sub=parser.add_subparsers(dest='action',required=True)
    cp=sub.add_parser('connections');cs=cp.add_subparsers(dest='connection_action',required=True)
    cs.add_parser('list')
    for name in ('save','check','remove'):
        item=cs.add_parser(name);item.add_argument('--kind',required=True)
        if name=='save':
            source=item.add_mutually_exclusive_group(required=True);source.add_argument('--file');source.add_argument('--values')
    for action in ('list','skills','help','add'):
        p=sub.add_parser(action);p.add_argument('--cwd',default=str(Path.cwd()))
        if action=='add':
            p.add_argument('--skill',required=True);p.add_argument('--label')
    p=sub.add_parser('remove');p.add_argument('--skill',required=True)
    p=sub.add_parser('configure')
    p.add_argument('--auto-servers',choices=['on','off']);p.add_argument('--auto-terminal',choices=['on','off'])
    p.add_argument('--launch-app',help='system(기본 PowerShell)' if IS_WINDOWS else 'system 또는 .command 실행을 지원하는 설치 앱의 전체 경로')
    args=parser.parse_args(argv);store=store or ActionStore(DATA_DIR / '.actions')
    cwd=Path(getattr(args,'cwd',Path.cwd())).expanduser().resolve()
    try:
        if not cwd.is_dir():raise ValueError('작업 폴더가 없습니다.')
        if args.action=='connections':
            action=args.connection_action
            if action=='list':result=store.connections.catalog()
            elif action=='save':result=store.connections.save(args.kind,json.loads(Path(args.file).read_text(encoding="utf-8") if args.file else args.values))
            elif action=='remove':result=store.connections.remove(args.kind)
            else:result=store.connections.check(args.kind)
            print(json.dumps(result,ensure_ascii=False,indent=2));return 0
        if args.action=='help':print(store.guide(cwd));return 0
        if args.action=='skills':result=store.catalog(cwd)
        elif args.action=='configure':
            changes={key:getattr(args,key)=='on' for key in ('auto_servers','auto_terminal') if getattr(args,key) is not None}
            if args.launch_app is not None:changes['launch_app']='' if args.launch_app=='system' else args.launch_app
            result=store.configure(changes)
        else:
            if args.action in ('add','remove'):store.favorite(args.skill,args.action=='add',cwd,getattr(args,'label',None))
            result={'settings':store.settings(),'shortcuts':store.shortcuts(cwd)}
        print(json.dumps(result,ensure_ascii=False,indent=2));return 0
    except (ValueError,OSError) as error:
        print(str(error),file=sys.stderr);return 2


if __name__=='__main__':sys.exit(main())
