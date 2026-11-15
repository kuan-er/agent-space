"""Read-only exact owned source check. Never registers/spawns/sends a task."""
import argparse
import json
from pathlib import Path
import re
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent/'src'))
from owned_source_product.records import read_json
from owned_source_product.registry import SourceContract

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--startup',type=Path,required=True)
    parser.add_argument('--binding',type=Path,required=True)
    parser.add_argument('--policy',type=Path,required=True)
    parser.add_argument('--source-key',required=True)
    args=parser.parse_args()
    # Only the explicit public client records are accepted, never node config,
    # KEL, credentials, private keys, tokens, or discovery of a running GUI.
    if args.startup.name!='startup.json' or args.binding.name!='source-binding.json':
        raise ValueError('explicit_owned_client_record_filenames_required')
    if args.policy.name!='release-policy.json':raise ValueError('explicit_release_policy_filename_required')
    checker=SourceContract(read_json(args.policy))
    result=checker.preflight(args.source_key,read_json(args.startup),read_json(args.binding))
    print(json.dumps(result,ensure_ascii=False));return 0

if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as error:
        reason=str(error)
        if not re.fullmatch('[a-z][a-z0-9_]{1,100}',reason):reason='owned_source_not_currently_verified'
        print(json.dumps({'operation':'read_only_source_preflight','binding':{'state':'unavailable'},
            'reason':reason,'error_type':type(error).__name__,'registry_written':False,
            'host_started':False,'model_turns':0,'a2a_tasks_sent':0},ensure_ascii=False))
        raise SystemExit(2)
