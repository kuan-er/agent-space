"""Small router inserted before the portal's generic JSON body reader."""
import json,re,threading
from urllib.parse import urlsplit
from portal_delivery import PortalDeliveries,Denied,MutationNotApplied,PART

_stores={};_lock=threading.Lock()
def storage(data):
    key=str(data.resolve())
    with _lock:
        if key not in _stores:_stores[key]=PortalDeliveries(data/'artifact-deliveries')
        return _stores[key]

def route(handler,method,db_factory,data,base):
    path=urlsplit(handler.path).path
    if path!='/api/node/artifacts' and not path.startswith('/api/node/artifacts/'):return False
    try:
        origin=handler.headers.get('Origin')
        if origin and origin!=base:raise Denied('cross-origin request refused')
        # Uses exactly the existing Handler.node bearer validation. No new account,
        # token, public URL, recipient impersonation, or device task admission.
        with db_factory() as c:
            node=handler.node(c);service=storage(data)
            if path=='/api/node/artifacts' and method=='POST':
                d=handler.body();op=d.get('op');mid=d.get('delivery_id')
                if op=='begin':result=service.begin(c,node,d.get('manifest'))
                elif op=='status':result=service.status(c,node,mid)
                elif op=='part_status':result=service.part_status(c,node,mid,d.get('file'),d.get('chunk'))
                elif op=='commit':result=service.commit(c,node,mid)
                elif op=='list':result={'deliveries':service.list_for_recipient(c,node)}
                elif op=='received':result=service.received(c,node,mid,d.get('files'))
                else:raise ValueError('unsupported artifact operation')
                handler.respond(result);return True
            match=re.fullmatch(r'/api/node/artifacts/part/([a-f0-9]{64})/(\d{1,3})/(\d{1,5})/(\d{1,3})',path)
            if match and method=='POST':
                length=int(handler.headers.get('Content-Length','0'))
                if not 0<length<=PART:raise ValueError('binary fragment exceeds 24 KiB')
                handler.connection.settimeout(15)
                body=handler.rfile.read(length)
                if len(body)!=length:raise ValueError('short fragment')
                result=service.upload_part(c,node,match[1],int(match[2]),int(match[3]),int(match[4]),body,handler.headers.get('X-Content-SHA256',''))
                handler.respond(result);return True
            match=re.fullmatch(r'/api/node/artifacts/read/([a-f0-9]{64})/(\d{1,3})/(\d{1,12})',path)
            if match and method=='GET':
                body,sha=service.download_part(c,node,match[1],int(match[2]),int(match[3]))
                handler.send_response(200);handler.send_header('Content-Type','application/octet-stream');handler.send_header('Content-Length',str(len(body)));handler.send_header('X-Content-SHA256',sha);handler.send_header('Cache-Control','no-store');handler.end_headers();handler.wfile.write(body);return True
            raise ValueError('unsupported artifact endpoint')
    except MutationNotApplied as e:
        handler.respond({'error':'artifact_mutation_not_applied','operation':e.operation,
            'manifest_sha256':e.mid,'effect':'not_applied','reason':e.reason},409);return True
    except Denied:
        handler.respond({'error':'artifact_access_denied'},403);return True
    except FileNotFoundError:
        handler.respond({'error':'artifact_not_found'},404);return True
    except (ValueError,KeyError,TypeError,IndexError) as error:
        reason={'invalid fragment':'fragment_validation','short fragment':'short_fragment',
            'binary fragment exceeds 24 KiB':'fragment_size','conflicting immutable fragment':'immutable_conflict',
            'chunk size/hash mismatch':'chunk_integrity','stored part size/type mismatch':'stored_fragment_integrity',
            'stored chunk corrupt':'stored_chunk_integrity'}.get(str(error),'request_or_integrity')
        body={'error':'artifact_integrity_or_request_failed'}
        if path.startswith('/api/node/artifacts/part/'):body['reason']=reason
        handler.respond(body,409);return True

def close_all():
    with _lock:
        for service in _stores.values():service.close()
        _stores.clear()
