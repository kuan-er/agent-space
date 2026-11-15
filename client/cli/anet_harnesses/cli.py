import argparse,json
from .catalog import probe_all,probe_one,INSTALL
def main():
    p=argparse.ArgumentParser();sub=p.add_subparsers(dest='action',required=True)
    q=sub.add_parser('probe');q.add_argument('--json',action='store_true');q.add_argument('--harness',choices=list(INSTALL));q.add_argument('--executable');q.add_argument('--config');q.add_argument('--node-root')
    sub.add_parser('install-catalog').add_argument('--json',action='store_true')
    a=p.parse_args()
    if a.action=='install-catalog':value={'schema':'anet-harness-install/1','entries':INSTALL}
    else:
        if a.executable and not a.harness:p.error('--executable requires --harness')
        if a.config or a.node_root:
            from .service import probe,load_config
            if a.harness or a.executable:p.error('--config uses the complete harness configuration')
            value=probe(load_config(a.config) if a.config else None,a.node_root)
        else:value=probe_one(a.harness,a.executable) if a.harness else probe_all()
    print(json.dumps(value,ensure_ascii=False,separators=(',',':')))
if __name__=='__main__':main()
