"""Version and fingerprint guards without any process access or native call."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import sys,hashlib,unittest
ROOT=Path(__file__).resolve().parent.parent;sys.path.insert(0,str(ROOT/'src'))
import native_client
from app_version import WECHAT_VERSION,NATIVE_DLL

class Tests(unittest.TestCase):
    def process(self,version=None,pid=123):
        module=SimpleNamespace(path='C:\\Weixin\\'+(version or WECHAT_VERSION)+'\\Weixin.dll',addr='10000-20000')
        return SimpleNamespace(pid=pid,info={'name':'Weixin.exe'},memory_maps=lambda **kw:[module],create_time=lambda:456)
    def test_exact_new_version_and_fingerprint_pass_without_native_calls(self):
        raw=b'audited fixture';digest=hashlib.sha256(raw).hexdigest()
        with patch('native_client.psutil.process_iter',return_value=[self.process()]),patch('native_client.Path.read_bytes',return_value=raw),patch('native_client.WECHAT_DLL_SHA256',digest):
            self.assertEqual(native_client.discover(),(123,456,0x10000))
        self.assertEqual(NATIVE_DLL,'MomentsNativeV8.dll')
    def test_same_version_changed_binary_is_blocked(self):
        with patch('native_client.psutil.process_iter',return_value=[self.process()]),patch('native_client.Path.read_bytes',return_value=b'changed'):
            with self.assertRaisesRegex(native_client.NativeError,'文件与已适配版本不一致'):native_client.discover()
    def test_old_or_future_version_is_not_force_accepted(self):
        for version in ('4.1.15.13','4.2.0.10'):
            with self.subTest(version=version),patch('native_client.psutil.process_iter',return_value=[self.process(version)]),patch('native_client.Path.read_bytes') as read:
                with self.assertRaisesRegex(native_client.NativeError,'暂未找到匹配'):native_client.discover()
                read.assert_not_called()
    def test_unreadable_binary_stops_before_native_call(self):
        with patch('native_client.psutil.process_iter',return_value=[self.process()]),patch('native_client.Path.read_bytes',side_effect=OSError()):
            with self.assertRaisesRegex(native_client.NativeError,'无法核对'):native_client.discover()
    def test_multiple_matching_processes_are_not_guessed(self):
        raw=b'audited fixture'
        with patch('native_client.psutil.process_iter',return_value=[self.process(pid=1),self.process(pid=2)]),patch('native_client.Path.read_bytes',return_value=raw),patch('native_client.WECHAT_DLL_SHA256',hashlib.sha256(raw).hexdigest()):
            with self.assertRaises(native_client.NativeError):native_client.discover()
if __name__=='__main__':unittest.main(verbosity=2)
