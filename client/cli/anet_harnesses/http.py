import json
from urllib.request import Request,build_opener,HTTPRedirectHandler
from urllib.error import HTTPError
from .transport import ProtocolError

class WorkBuddyUnauthorized(ProtocolError):
    """A POST has already been sent; refreshing auth never replays that POST."""
    status=401

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*a,**kw):raise ProtocolError('authenticated redirect refused')

class WorkBuddyHttp:
    """Token is supplied in memory by the existing portal OAuth broker.

    No frontend client_secret, credential file or automatic POST auth retry.
    """
    def __init__(self,token_supplier,timeout=15):self.token_supplier=token_supplier;self.timeout=timeout
    def _validate(self,method,path):
        if method not in ('GET','POST') or not (path=='/tasks' or path.startswith('/tasks/') or path=='/localassistant'):
            raise ValueError('endpoint outside finite adapter contract')
        if any(c in path for c in '?&#\\\r\n') or '..' in path:raise ValueError('invalid API path')
    def preflight(self):
        token=self.token_supplier()
        if not isinstance(token,str) or not token or any(c.isspace() for c in token):raise ProtocolError('OAuth authorization unavailable')
        return token
    def prepare(self,method,path,payload=None):
        """Resolve authorization before the caller commits a mutation intent.

        This exact RAM token is used by the one send, so a second broker lookup
        cannot turn an unattempted task into a permanently uncertain operation.
        """
        self._validate(method,path);token=self.preflight()
        return lambda:self._send(method,path,payload,token)
    def __call__(self,method,path,payload=None):
        return self.prepare(method,path,payload)()
    def _send(self,method,path,payload,token,refresh_allowed=True):
        body=json.dumps(payload,ensure_ascii=False).encode() if payload is not None else None
        request=Request('https://www.workbuddy.cn/openapi/v2'+path,data=body,method=method,headers={'Authorization':'Bearer '+token,'Content-Type':'application/json','Accept':'application/json'})
        try:
            with build_opener(NoRedirect).open(request,timeout=self.timeout) as r:raw=r.read(2097153)
        except HTTPError as e:
            status=e.code;e.close()
            if status==401:
                updated=None
                if refresh_allowed and hasattr(self.token_supplier,'refresh'):
                    try:updated=self.token_supplier.refresh(token)
                    except Exception:pass
                if method=='GET' and isinstance(updated,str) and updated:
                    return self._send(method,path,payload,updated,refresh_allowed=False)
                raise WorkBuddyUnauthorized('WorkBuddy authorization expired; original write was not repeated') from None
            raise ProtocolError('WorkBuddy HTTP '+str(status)+'; effect must be reconciled before another write') from None
        if len(raw)>2097152:raise ProtocolError('WorkBuddy response too large')
        result=json.loads(raw)
        if not isinstance(result,dict):raise ProtocolError('invalid WorkBuddy response')
        if 'code' in result:
            if result['code']!=0:raise ProtocolError('WorkBuddy application refused request')
            result=result.get('data')
        if not isinstance(result,dict):raise ProtocolError('invalid WorkBuddy data')
        return result
