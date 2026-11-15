"""Finite local-owner policy entry. Not an RPC/remote-answer grant protocol."""
import argparse,json,sys
from pathlib import Path
from anet_harnesses.adapters import identifier
from .controller import GoalHold
from .owner_policy import OwnerPolicy,ReadOnlyLedger,PolicyWriteUncertain

def dispatch(state_root,workspace,request):
    if (not isinstance(request,dict) or set(request)-{'action','source_id','native_id','user_authorized'}
        or request.get('action') not in ('status','grant','revoke')):raise GoalHold('finite_local_owner_policy_request_required')
    if type(request.get('user_authorized',False)) is not bool:raise GoalHold('user_authorized_must_be_boolean')
    root=Path(state_root);work=Path(workspace)
    if not root.is_absolute() or not work.is_absolute():raise GoalHold('absolute_owner_runtime_paths_required')
    policy=OwnerPolicy(ReadOnlyLedger(root),root)
    source=identifier(request.get('source_id'));native=identifier(request.get('native_id'))
    if request['action']=='status':return policy.status(source,native,work)
    return policy.set_local(source,native,work,granted=request['action']=='grant',user_authorized=request.get('user_authorized',False))

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--state-root',required=True);parser.add_argument('--workspace',required=True);args=parser.parse_args()
    request=None
    try:
        raw=sys.stdin.buffer.read(16385)
        if len(raw)>16384:raise GoalHold('owner_policy_request_too_large')
        request=json.loads(raw.decode('utf-8-sig'))
        result=dispatch(args.state_root,args.workspace,request)
        reply={'ok':True,'result':result};code=0
    except Exception as error:
        reply={'ok':False,'error':type(error).__name__,'reason_code':str(error) if isinstance(error,GoalHold) else 'owner_policy_outcome_unverified',
               'policy_effect_status':'NO_EFFECT' if isinstance(error,GoalHold) and not isinstance(error,PolicyWriteUncertain) else 'UNVERIFIED',
               'retry_authorized':False};code=1
        # Bind a definite rejection to this one local owner's finite request.
        # The UI must compare all three fields, never infer rejection from text.
        for key in ('action','source_id','native_id'):
            value=request.get(key) if isinstance(request,dict) else None
            reply[key]=value if isinstance(value,str) and len(value)<=512 else None
    print(json.dumps(reply,ensure_ascii=False),flush=True)
    return code

if __name__=='__main__':raise SystemExit(main())
