import hashlib
import re

def enroll(m,c,u,d):
    """Idempotent account enrollment; signed node proof remains mandatory."""
    aid=d.get('aid');name=d.get('name');executor=d.get('executor','codex');pt=d.get('pair_token');requested=d.get('agent_id');kind=d.get('kind','device')
    if not isinstance(aid,str) or not m.AID.fullmatch(aid):raise m.Problem('节点身份无效')
    if not isinstance(pt,str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}',pt):raise m.Problem('连接凭证无效')
    if kind not in ('device','agent') or (kind=='device' and executor!='codex'):raise m.Problem('连接类型无效')
    if not isinstance(name,str) or not 1<=len(name.strip())<=60 or executor not in m.EXECUTORS:raise m.Problem('名称或智能体类型无效')
    bound=m.one(c,'SELECT * FROM agents WHERE aid=? OR (candidate_aid=? AND pair_expires>?)',(aid,aid,m.now()))
    if bound and bound['owner_id']!=u['id']:raise m.Problem('节点已属于其他账户',409)
    if requested:
        a=m.one(c,'SELECT * FROM agents WHERE id=? AND owner_id=?',(str(requested),u['id']))
        if not a:raise m.Problem('连接不属于当前账户',403)
        if (a['aid'] and a['aid']!=aid) or (a['candidate_aid'] and a['candidate_aid']!=aid):raise m.Problem('必须保留原节点身份',409)
        if bound and bound['id']!=a['id']:raise m.Problem('节点已有其他连接',409)
    elif bound:a=bound
    else:
        # Stable per owner and local identity. A lost response cannot create another record.
        identifier=hashlib.sha256(('anet-client/1:'+u['id']+':'+aid).encode()).hexdigest()[:24]
        a=m.one(c,'SELECT * FROM agents WHERE id=?',(identifier,))
        if a and a['owner_id']!=u['id']:raise m.Problem('连接冲突',409)
        if not a:
            if c.execute("SELECT COUNT(*) FROM agents WHERE owner_id=? AND kind=?",(u['id'],kind)).fetchone()[0]>=10:raise m.Problem('每类最多连接 10 台设备')
            c.execute("INSERT INTO agents(id,owner_id,name,description,visibility,kind,executor,created_at,updated_at) VALUES(?,?,?,'','private',?,?,?,?)",(identifier,u['id'],name.strip(),kind,executor,m.now(),m.now()))
            m.event(c,u['id'],'agent.created',identifier)
            a=m.one(c,'SELECT * FROM agents WHERE id=?',(identifier,))
    if not a['enabled']:raise m.Problem('连接已停用',403)
    # Retrying the same request must retain the in-flight signed challenge.
    if a['pair_hash']!=m.digest(pt):
        c.execute('UPDATE agents SET pair_hash=?,pair_expires=?,challenge=NULL WHERE id=?',(m.digest(pt),m.now()+3600,a['id']))
    elif not a['pair_expires'] or a['pair_expires']<=m.now():
        c.execute('UPDATE agents SET pair_expires=? WHERE id=?',(m.now()+3600,a['id']))
    c.commit()
    return {'protocol':'anet-client/1','agent_id':a['id'],'kind':a['kind'],'executor':a['executor'],'hub':m.HUB,'recovery':bool(a['aid'])}
