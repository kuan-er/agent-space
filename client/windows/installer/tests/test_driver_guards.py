"""Only fresh tiny files; no runtime, native agent, network, or build."""
import hashlib,json,os,tempfile,unittest
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from held_inputs import HeldInputs,metadata,no_reparse

class HeldInputTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(prefix='tiny-hold-')
  self.root=Path(self.tmp.name);self.p=self.root/'input.bin';self.p.write_bytes(b'held-public-fixture\n'*32)
  self.row={'path':str(self.p),'sha256':hashlib.sha256(self.p.read_bytes()).hexdigest(),'security_sddl':'synthetic',**metadata(self.p.stat())}
 def tearDown(self):self.tmp.cleanup()
 def test_hold_blocks_write_and_path_replacement(self):
  with HeldInputs([self.row],lambda p:'synthetic'):
   with self.assertRaises(PermissionError):self.p.write_bytes(b'changed')
   with self.assertRaises(PermissionError):self.p.rename(self.root/'renamed')
   with self.assertRaises(PermissionError):self.p.unlink()
  self.assertEqual(self.p.read_bytes(),b'held-public-fixture\n'*32)
 def test_read_share_allows_build_hardlink(self):
  with HeldInputs([self.row],lambda p:'synthetic'):
   target=self.root/'runtime-link.bin';os.link(self.p,target)
   self.assertEqual(self.p.stat().st_ino,target.stat().st_ino)
   self.assertEqual(target.read_bytes(),self.p.read_bytes())
 def test_hash_drift_holds_before_body(self):
  self.row['sha256']='0'*64
  with self.assertRaisesRegex(ValueError,'pin_drift'):
   with HeldInputs([self.row],lambda p:'synthetic'):self.fail('unreachable')
  self.p.write_bytes(b'handles closed after rejection')
 def test_security_drift_holds(self):
  with self.assertRaisesRegex(ValueError,'acl_drift'):
   with HeldInputs([self.row],lambda p:'different'):self.fail('unreachable')
 def test_identity_drift_holds(self):
  self.row['file_id']+=1
  with self.assertRaisesRegex(ValueError,'pin_drift'):
   with HeldInputs([self.row],lambda p:'synthetic'):self.fail('unreachable')
 def test_relative_path_rejected(self):
  with self.assertRaisesRegex(ValueError,'absolute'):no_reparse(Path('relative.bin'))
 def test_duplicate_path_held_once(self):
  with HeldInputs([self.row,self.row],lambda p:'synthetic') as hold:self.assertEqual(len(hold.handles),1)

if __name__=='__main__':unittest.main(verbosity=2)
