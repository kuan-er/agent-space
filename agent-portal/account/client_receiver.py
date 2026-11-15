"""Enable the exact authenticated device without changing its identity or trust."""
def enable(m,c,user,data):
    identifier,aid,executor=(data.get(key) for key in ('agent_id','aid','executor'))
    if data.get('confirm') is not True or executor not in ('codex','claude','deepseek'):
        raise m.Problem('请选择支持的软件，并确认开启接收任务。')
    if not isinstance(identifier,str) or not isinstance(aid,str):
        raise m.Problem('连接身份无效。')
    row=m.one(c,'SELECT * FROM agents WHERE id=? AND owner_id=?',(identifier,user['id']))
    if not row or not row['enabled']:
        raise m.Problem('连接不属于当前账户或已停用。',403)
    if row['aid']!=aid or not row['node_hash']:
        raise m.Problem('设备身份尚未完成核验。',409)
    if row['kind']=='agent':
        if row['executor']!=executor:
            raise m.Problem('设备已有其他执行器，请先在客户端检查。',409)
        return {'ok':True,'id':identifier,'kind':'agent','executor':executor,'needs_local_setup':not bool(row['executor_ready'])}
    if row['kind']!='device':raise m.Problem('连接类型不支持此操作。',409)
    if c.execute("SELECT COUNT(*) FROM agents WHERE owner_id=? AND kind='agent'",(user['id'],)).fetchone()[0]>=10:
        raise m.Problem('可接收任务的连接最多 10 个。')
    c.execute("UPDATE agents SET kind='agent',executor=?,policy_ok=0,executor_ready=0,capabilities=NULL,runtime=NULL,pair_hash=NULL,pair_expires=CASE WHEN candidate_aid IS NOT NULL THEN 0 ELSE NULL END,challenge=NULL,updated_at=? WHERE id=?",(executor,m.now(),identifier))
    m.event(c,user['id'],'connection.executor_enabled',identifier)
    return {'ok':True,'id':identifier,'kind':'agent','executor':executor,'needs_local_setup':True}
