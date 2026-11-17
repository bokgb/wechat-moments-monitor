"""Exercise live reader's extraction path with synthetic cipher objects."""
from pathlib import Path
from unittest.mock import patch
import ctypes,hashlib,hmac,struct,sys,tempfile,unittest

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT/'src'))
from read_backend import db as backend

class CipherPassphraseTests(unittest.TestCase):
    def extract(self,decoded,page):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'contact.db';path.write_bytes(page)
            reader=backend.WeChatDB.__new__(backend.WeChatDB)
            reader._db_files=[('contact/contact.db',str(path),len(page))]
            name_address,node_address,config_address,data_address=0x10000,0x20000,0x30000,0x40000
            node=bytearray(80)
            struct.pack_into('<QQ',node,16,name_address,len(backend.CONFIG_CIPHER_NAME))
            struct.pack_into('<Q',node,40,config_address)
            obj=bytearray(40);struct.pack_into('<QQ',obj,8,data_address,len(decoded))
            blob=bytes(value^backend.CONFIG_XOR_MASK[i%32] for i,value in enumerate(decoded))
            memory={node_address:bytes(node),config_address+0x88:bytes(obj),data_address:blob}
            def read_memory(handle,address,buffer,length,count):
                data=memory.get(address.value,b'')[:length]
                if not data:return False
                ctypes.memmove(buffer,data,len(data))
                ctypes.cast(count,ctypes.POINTER(ctypes.c_size_t))[0]=len(data)
                return True
            with patch.object(reader,'_find_bytes',side_effect=[[name_address],[node_address+16]]),\
                 patch.object(backend._k32,'OpenProcess',return_value=123),\
                 patch.object(backend._k32,'CloseHandle'),\
                 patch.object(backend._k32,'ReadProcessMemory',side_effect=read_memory):
                return reader._extract_keys_pid(123,set())

    def page(self,key):
        salt=bytes(range(16));page=bytearray(salt+bytes(backend.PAGE_SZ-16))
        mac_key=backend._pbkdf2(key,bytes(value^0x3a for value in salt),2)
        message=bytes(page[16:backend.PAGE_SZ-backend.RESERVE_SZ+16])+struct.pack('<I',1)
        page[-64:]=hmac.new(mac_key,message,hashlib.sha512).digest()
        return bytes(page)

    def test_binary_passphrase_is_derived_and_verified(self):
        password=hashlib.sha256(b'synthetic cipher password').digest()
        expected=backend._pbkdf2(password,bytes(range(16)),256000)
        self.assertEqual(self.extract(password,self.page(expected)),{'contact/contact.db':expected})

    def test_existing_raw_key_literal_still_works(self):
        key=hashlib.sha256(b'synthetic raw key').digest()
        literal=("x'"+key.hex()+bytes(range(16)).hex()+"'").encode()
        self.assertEqual(self.extract(literal,self.page(key)),{'contact/contact.db':key})

    def test_wrong_passphrase_cannot_bypass_page_verification(self):
        password=hashlib.sha256(b'wrong synthetic password').digest()
        key=hashlib.sha256(b'unrelated synthetic key').digest()
        self.assertEqual(self.extract(password,self.page(key)),{})

if __name__=='__main__':unittest.main()
