from contextlib import closing
import importlib.util,json,sqlite3,tempfile,unittest
from pathlib import Path
spec=importlib.util.spec_from_file_location('migration_portal',Path(__file__).parents[1]/'server.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class MigrationTest(unittest.TestCase):
 def test_existing_agent_identity_and_executor_state_preserved(self):
  with tempfile.TemporaryDirectory() as folder:
   m.DATA=Path(folder)
   with closing(sqlite3.connect(m.DATA/'portal.sqlite3')) as c, c:
    c.executescript('''CREATE TABLE agents(id TEXT PRIMARY KEY, owner_id TEXT, name TEXT NOT NULL, description TEXT NOT NULL,visibility TEXT NOT NULL DEFAULT 'private',aid TEXT UNIQUE,enabled INTEGER DEFAULT 1,pair_hash TEXT,pair_expires INTEGER,challenge TEXT,candidate_aid TEXT,provisioned INTEGER DEFAULT 0,node_hash TEXT,last_seen INTEGER,policy_ok INTEGER DEFAULT 0,executor_ready INTEGER DEFAULT 0,created_at INTEGER,updated_at INTEGER);''')
    c.execute('INSERT INTO agents(id,owner_id,name,description,aid,node_hash,last_seen,policy_ok,executor_ready,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',('existing','owner','EPS','unchanged','existing-aid','existing-token-hash',123456,1,1,100,200))
   m.init();m.init()
   with m.db() as c:a=m.one(c,'SELECT * FROM agents WHERE id=?',('existing',))
   self.assertEqual(a['kind'],'agent');self.assertEqual(a['executor'],'codex');self.assertEqual(a['aid'],'existing-aid');self.assertEqual(a['node_hash'],'existing-token-hash');self.assertEqual(a['last_seen'],123456);self.assertEqual(a['policy_ok'],1);self.assertEqual(a['executor_ready'],1)
if __name__=='__main__':unittest.main(verbosity=2)
