"""Explicit desktop lifecycle; owned source + existing daemon, no fallback send.

Hello never starts a broker. Create is explicit, register journals its UUID before
handoff, and uncertain registrations are reconciled using the original receipt.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/'src'))
sys.path.insert(0,str(ROOT/'broker/src'))
from owned_source_product.controller import ProductController,PROTOCOL as FORM_PROTOCOL
from owned_source_product.profiles import PRODUCT
from owned_source_product.records import atomic_json,read_json,utcnow
from owned_source_product.submissions import ExistingBindingGateway,ProductSubmissions,PROTOCOL
from owned_source_product import private_directory as private

class DesktopController:
    def __init__(self,node_root,*,gateway=None,host=None,runtime=None):
        self.gateway=gateway or ExistingBindingGateway(node_root)
        self.host=host
        if host is None:
            import launcher,winlocal
            self.host=launcher;self.ipc=winlocal
        else:self.ipc=host
        self.process=None;self.startup=None;self.binding=None;self.form=None;self.closed=False
        self.creation_id=str(uuid.uuid4());self.source_key=str(uuid.uuid4())
        if runtime is None:
            data_root=Path(os.environ.get('ANET_SOURCE_DESKTOP_ROOT',str(ROOT/'desktop-state')))
            if not data_root.is_absolute():raise ValueError('absolute_source_state_root_required')
            if os.environ.get('ANET_SOURCE_DESKTOP_ROOT'):
                self.ipc.ensure_private_runtime_root(data_root.parent)
            root=self.ipc.ensure_private_runtime_root(data_root)
            runtime=self.ipc.create_private_directory(root/str(uuid.uuid4()))
        self.runtime=Path(runtime)
        self.state={'protocol':'owned-source-desktop/1','phase':'not_started','source_key':self.source_key,
            'source_request_id':self.creation_id,'runtime':str(self.runtime),'source':None,'registration_request':None,
            'model_turns_requested_by_desktop':0,'recovery_generation':0,'at':utcnow()}
        self.save()
    def save(self):atomic_json(self.runtime/'desktop.json',self.state)
    def view(self):
        return dict(copy.deepcopy(self.state),service_available=self.gateway.available(),
            source_ready=self.startup is not None and self.binding is not None,
            registered=self.state.get('source') is not None,
            create_enabled=self.state['phase']=='not_started',
            register_enabled=self.binding is not None and self.state.get('registration_request') is None and self.gateway.available(),
            open_task_enabled=self.form is not None,
            actions=self.form.submissions.list_actions() if self.form else [],
            recovery_candidates=self.recovery_candidates(),recover_enabled=self.state['phase']=='not_started')
    def recovery_candidates(self):
        values={}
        directories=list(self.runtime.parent.iterdir())
        if len(directories)>128:return []
        for path in directories:
            if path==self.runtime or not path.is_dir():continue
            try:
                if str(uuid.UUID(path.name))!=path.name:continue
                private.verify(path,root=True);old=read_json(path/'desktop.json') or {}
                source=self.recorded_source(old) or {}
                if source.get('source_purpose')==PRODUCT and source.get('binding',{}).get('record_sha256'):
                    key=source.get('source_key');generation=old.get('recovery_generation',0)
                    if type(generation) is not int or generation<0:continue
                    value={'runtime_id':path.name,'session':source['session'],'phase':old.get('phase'),'generation':generation}
                    if key not in values or generation>values[key]['generation']:values[key]=value
            except (OSError,ValueError,KeyError):continue
        return list(values.values())
    def recorded_source(self,state):
        if state.get('source'):return state['source']
        request=state.get('registration_request')
        if not isinstance(request,dict):return None
        receipt=self.gateway.lookup(request)
        if receipt is None or receipt.get('state')!='verified':return None
        from owned_source_product.submissions import fingerprint
        if (receipt.get('protocol')!=PROTOCOL or receipt.get('request_id')!=request.get('request_id')
            or receipt.get('request_sha256')!=fingerprint(request)):
            raise ValueError('prior_registration_receipt_identity_mismatch')
        return receipt.get('result')
    def accept_source(self,value):
        self.binding=value['binding']
        if self.binding.get('purpose')!=PRODUCT:raise ValueError('product_source_purpose_required')
        self.state.update(phase='source_ready',session=self.binding['session'],cwd=self.binding['cwd'])
        atomic_json(self.runtime/'source-binding.json',self.binding);self.save()
    def create(self):
        if self.state.get('recovery'):raise ValueError('owned_recovery_in_progress_no_replacement_source')
        if self.startup is None:
            self.state['phase']='starting_owned_host';self.save()
            self.process,self.startup=self.host.launch()
            atomic_json(self.runtime/'startup.json',self.startup)
        if self.binding is None:
            if self.state.get('creation_requested'):
                value=self.ipc.request(self.startup,'inspect_creation',source_request_id=self.creation_id)
                if value.get('state')!='accepted':
                    self.state.update(phase='source_creation_unconfirmed',creation_state=value.get('state'));self.save();return self.view()
            else:
                self.state.update(phase='creating_owned_source',creation_requested=True);self.save()
                value=self.ipc.request(self.startup,'create_source',source_request_id=self.creation_id,purpose=PRODUCT,source_key=self.source_key)
            self.accept_source(value)
        return self.view()
    def consume_registration(self,receipt):
        request=self.state['registration_request']
        from owned_source_product.submissions import fingerprint,source_projection
        if (receipt.get('protocol')!=PROTOCOL or receipt.get('request_id')!=request['request_id']
            or receipt.get('request_sha256')!=fingerprint(request)):raise ValueError('registration_receipt_identity_mismatch')
        self.state['registration_receipt']=receipt
        if receipt.get('state')!='verified':
            self.state['phase']='registration_rejected';self.save();return
        source=receipt.get('result') or {};projected=source_projection(source)
        if (projected['session']!=self.binding['session'] or projected['cwd']!=self.binding['cwd']
            or source.get('source_key')!=self.source_key):raise ValueError('registration_source_mismatch')
        self.state.update(source=source,phase='registered')
        atomic_json(self.runtime/'source-receipt.json',source)
        directory=Path(self.state.get('submission_root') or self.runtime/'submissions')
        self.form=ProductController(ProductSubmissions(directory,self.gateway),source);self.save()
    def register(self):
        if self.binding is None:raise ValueError('create_explicit_owned_source_first')
        if self.state.get('source'):return self.view()
        prior=self.state.get('registration_request')
        if prior:
            found=self.gateway.lookup(prior)
            if found is not None:self.consume_registration(found)
            return self.view()
        if not self.gateway.available():raise ValueError('owned_source_service_not_advertised_no_handoff')
        request={'protocol':PROTOCOL,'request_id':str(uuid.uuid4()),'operation':'register_owned_source',
            'created_at':utcnow(),'expires_at':time.time()+40,
            'payload':{'source_key':self.source_key,'startup':self.startup,'binding':self.binding}}
        if self.state.get('recovery'):
            request['operation']='recover_owned_source'
            request['payload']['expected_record_sha256']=self.state['recovery']['expected_record_sha256']
        self.state.update(phase='registration_requested',registration_request=request);self.save()
        try:self.consume_registration(self.gateway.register_source(request))
        except Exception:
            self.state.update(phase='registration_unconfirmed',instruction='Query the same registration request; do not create another source.');self.save();raise
        return self.view()
    def refresh(self):
        if self.state.get('registration_request') and not self.state.get('source'):self.register()
        if self.startup and self.binding:
            observed=self.ipc.request(self.startup,'inspect_source',binding=self.binding)
            self.state['source_observation']=observed
        elif self.startup:
            if self.state.get('recovery'):self.recover(self.state['recovery']['prior_runtime_id'])
            else:self.create() # inspect_creation only after requested
        self.state['at']=utcnow();self.save();return self.view()
    def recover(self,prior_runtime_id):
        if str(uuid.UUID(prior_runtime_id))!=prior_runtime_id:raise ValueError('explicit_prior_owned_runtime_required')
        recovery=self.state.get('recovery')
        if recovery and recovery.get('prior_runtime_id')!=prior_runtime_id:raise ValueError('recovery_origin_change_refused')
        if not recovery:
            if self.state['phase']!='not_started':raise ValueError('recovery_requires_new_desktop_instance')
            prior=private.plain(self.runtime.parent/prior_runtime_id);private.verify(prior,root=True)
            old=read_json(prior/'desktop.json') or {};source=self.recorded_source(old) or {}
            if not source:raise ValueError('prior_registration_unconfirmed_query_original_receipt')
            from owned_source_product.submissions import source_projection
            source_projection(source)
            startup=read_json(prior/'startup.json');binding=read_json(prior/'source-binding.json')
            if source.get('source_key')!=old.get('source_key') or source.get('session')!=binding.get('session'):
                raise ValueError('prior_owned_source_receipt_mismatch')
            directory=private.plain(old.get('submission_root') or prior/'submissions')
            if directory.name!='submissions' or directory.parent.parent!=self.runtime.parent:
                raise ValueError('prior_owned_submission_directory_required')
            if directory.exists():private.verify(directory,root=True)
            elif old.get('source') or old.get('submission_root'):
                raise ValueError('original_submission_journal_missing_hold')
            else:directory=self.runtime/'submissions' # no form existed before the original registration ACK
            recovery={'prior_runtime_id':prior_runtime_id,'recovery_id':str(uuid.uuid4()),
                'session':source['session'],'source_key':source['source_key'],'prior_state_dir':startup['state_dir'],
                'expected_record_sha256':source['binding']['record_sha256']}
            self.source_key=source['source_key'];self.state.update(source_key=self.source_key,recovery=recovery,
                submission_root=str(directory),phase='recovery_prepared',recovery_generation=old.get('recovery_generation',0)+1);self.save()
        if self.startup is None:
            self.process,self.startup=self.host.launch();atomic_json(self.runtime/'startup.json',self.startup)
        if self.binding is None:
            # Broker pins this recovery ID before one resume. Repeated checks
            # observe its original resume outcome and pending delivery journals.
            self.state['phase']='recovery_requested';self.save()
            arguments={k:recovery[k] for k in ('recovery_id','session','source_key','prior_state_dir','expected_record_sha256')}
            value=self.ipc.request(self.startup,'recover_source',**arguments)
            self.accept_source(value);self.state['phase']='recovered_requires_registration';self.save()
        return self.view()
    def close(self):
        if self.form:self.form.submissions.close_ui()
        if self.process and self.process.poll() is None:self.host.stop(self.process,self.startup)
        self.closed=True;self.state.update(phase='closed',closed_at=utcnow());self.save()
        return self.view()
    def handle(self,command):
        if command.get('protocol')==FORM_PROTOCOL:
            if self.form is None:raise ValueError('explicit_product_source_registration_required')
            if self.form.closed and command.get('operation') in ('hello','open_original'):
                self.form=ProductController(self.form.submissions,self.state['source'])
            return self.form.handle(command)
        if command.get('protocol')!='owned-source-desktop/1':raise ValueError('desktop_protocol_required')
        if self.closed:raise ValueError('desktop_already_closed')
        op=command.get('operation')
        if op=='hello':return self.view()
        if op=='create':return self.create()
        if op=='recover':return self.recover(command.get('prior_runtime_id'))
        if op=='register':return self.register()
        if op=='refresh':return self.refresh()
        if op=='close':return self.close()
        raise ValueError('unsupported_desktop_operation')

def serve(controller):
    limit=262144
    while True:
        line=sys.stdin.buffer.readline(limit+1)
        if not line:break
        if len(line)>limit or not line.endswith(b'\n'):break
        rid=None
        try:
            request=json.loads(line);rid=request.get('id')
            if not isinstance(rid,str):raise ValueError('request_id_required')
            reply={'id':rid,'ok':True,'result':controller.handle(request['command'])}
        except Exception as error:
            reply={'id':rid,'ok':False,'error_type':type(error).__name__,
                'reason':str(error)[:350],'instruction':'Preserve original source/action IDs; refresh original receipts.'}
        sys.stdout.buffer.write((json.dumps(reply,ensure_ascii=False)+'\n').encode('utf-8'));sys.stdout.buffer.flush()
        if controller.closed:return
    # EOF only affects this explicitly owned local host. Completed remote task
    # records remain in the existing daemon and are never canceled or replayed.
    controller.state.update(client_disconnected=True,at=utcnow());controller.save()

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--node-root',required=True);args=parser.parse_args()
    import winlocal
    lifetime=winlocal.own_lifetime_job()
    serve(DesktopController(args.node_root))

if __name__=='__main__':main()
