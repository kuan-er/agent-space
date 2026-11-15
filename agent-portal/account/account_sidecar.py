"""Narrow account enrollment API; original portal and its workers remain running."""
import argparse
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
from urllib.parse import urlsplit
from client_enrollment import enroll
from client_receiver import enable as enable_receiver


def handler(m):
    class AccountHandler(m.Handler):
        server_version='ANetAccount/1'
        def setup(self):
            super().setup();self.connection.settimeout(20)
        def dispatch(self,method):
            try:
                path=urlsplit(self.path)
                if path.query or path.fragment:raise m.Problem('接口不存在',404)
                if path.path=='/healthz' and method=='GET':return self.respond({'ok':True,'service':'anet-account'})
                allowed={'/api/client/config':'GET','/api/client/enroll':'POST','/api/client/receiver':'POST'}
                if path.path not in allowed:raise m.Problem('接口不存在',404)
                if method!=allowed[path.path]:raise m.Problem('请求方法无效',405)
                if self.headers.get('Transfer-Encoding'):raise m.Problem('请求格式错误')
                if self.headers.get('Origin') and self.headers['Origin']!=m.BASE:raise m.Problem('不接受跨站请求',403)
                if method=='POST':
                    try:length=int(self.headers.get('Content-Length','0'))
                    except ValueError:raise m.Problem('请求格式错误')
                    if length<0 or length>32768:raise m.Problem('请求内容过大',413)
                    self.rate('client-enroll',120);data=self.body()
                else:data={}
                with m.db() as c:
                    c.execute('PRAGMA busy_timeout=5000')
                    if method=='POST':c.execute('BEGIN IMMEDIATE')
                    try:
                        user=self.user(c,method=='POST')
                        if method=='GET':result={'protocol':'anet-client/1','hub':m.HUB}
                        elif path.path=='/api/client/receiver':result=enable_receiver(m,c,user,data)
                        else:result=enroll(m,c,user,data)
                    except BaseException:
                        c.rollback();raise
                return self.respond(result)
            except m.Problem as e:self.respond({'error':e.message},e.status)
            except sqlite3.IntegrityError:self.respond({'error':'连接已存在，请使用原连接重试。'},409)
            except (sqlite3.OperationalError,TimeoutError):self.respond({'error':'服务暂忙，请稍后重试。'},503)
            except (BrokenPipeError,ConnectionResetError):pass
            except Exception:self.respond({'error':'配置尚未完成，请稍后重试。'},500)
    return AccountHandler


def main():
    p=argparse.ArgumentParser();p.add_argument('--portal-code',required=True);p.add_argument('--port',type=int,default=8891);a=p.parse_args()
    source=Path(a.portal_code).resolve();sys.path.insert(0,str(source))
    spec=importlib.util.spec_from_file_location('anet_account_portal',source/'server.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    if not (m.DATA/'portal.sqlite3').is_file():raise SystemExit('existing portal database required')
    m.ThreadingHTTPServer(('127.0.0.1',a.port),handler(m)).serve_forever()


if __name__=='__main__':main()
