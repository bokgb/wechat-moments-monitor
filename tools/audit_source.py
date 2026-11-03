"""Audit the shareable source tree. Does not read WeChat or upload anything."""
from pathlib import Path
import hashlib,json,re,sys
ROOT=Path(__file__).resolve().parent.parent
IGNORE={'.git','.venv','venv','__pycache__','build','dist','.pytest_cache'}
TOP={'src','native','tests','tools','docs','LICENSES','.github','README.md','LICENSE','NOTICE','.gitignore','requirements.txt','requirements-build.txt'}
GENERATED={'.dll','.exe','.obj','.pdb','.exp','.lib'}
FORBIDDEN={'.db','.sqlite','.sqlite3','.log','.zip','.pem','.key','.png','.jpg','.jpeg'}
PATTERNS={
    'personal_user_path':re.compile(r'(?i)[a-z]:[/\\]+users[/\\]+(?!public\b|default\b)[^/\\\s]+'),
    'real_account':re.compile(r'wxid_(?!xxx(?:\b|_))[A-Za-z0-9_]{9,}'),
    'api_key':re.compile(r'\bsk-[A-Za-z0-9_-]{16,}'),
    'access_token':re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})'),
    'private_key':re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
}
def entries(root=ROOT):
    for path in sorted(root.rglob('*')):
        relative=path.relative_to(root)
        if IGNORE.intersection(relative.parts):continue
        if relative.as_posix()=='native/build.cmd':continue
        if relative.parts[0]=='native' and path.suffix.lower() in GENERATED:continue
        if path.is_file():yield path,relative
def audit(root=ROOT):
    files=[];issues=[]
    for path,relative in entries(root):
        name=relative.as_posix()
        if path.is_symlink():issues.append((name,'symlink'));continue
        if relative.parts[0] not in TOP:issues.append((name,'unexpected_file'))
        if 'data' in relative.parts or path.suffix.lower() in FORBIDDEN or '.sqlite' in path.name.lower() or path.name.endswith(('.request.json','.result.json')):issues.append((name,'runtime_data'))
        try:raw=path.read_bytes();value=raw.decode('utf-8')
        except UnicodeError:issues.append((name,'non_text_source'));continue
        for rule,pattern in PATTERNS.items():
            if pattern.search(value):issues.append((name,rule))
        files.append({'path':name,'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)})
    return {'files':files,'issues':issues}
if __name__=='__main__':
    report=audit();print(json.dumps({'files':len(report['files']),'issues':report['issues']},ensure_ascii=False))
    raise SystemExit(bool(report['issues']))
