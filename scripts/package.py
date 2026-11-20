"""Build self-contained deployment archives without config, database or credentials."""
from pathlib import Path
import hashlib
import io
import tarfile
import zipfile
import sys

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from fxwatch import __version__

files = [root/name for name in ('README.md', 'config.example.toml', '.gitignore',
                                '.gitattributes', 'Dockerfile', '.dockerignore', '.env.example',
                                'requirements-docker.txt', 'compose.yaml', 'deploy/config.docker.toml',
                                'docs/docker.md', 'docs/dockerhub.md', 'docs/verification.md', 'docs/rules.md')]
for folder, pattern in [('fxwatch','*.py'), ('fxwatch/static','*'), ('tests','*.py'),
                        ('tests/fixtures','*.txt'), ('scripts','*.py'), ('scripts','*.sh')]:
    files.extend(p for p in (root/folder).glob(pattern) if p.is_file())
files = sorted(set(files))
dist = root/'dist'
dist.mkdir(exist_ok=True)
base = f'fxwatch-{__version__}'
with tarfile.open(dist/f'{base}.tar.gz','w:gz') as tar, zipfile.ZipFile(dist/f'{base}.zip','w',zipfile.ZIP_DEFLATED) as archive:
    for path in files:
        relative = path.relative_to(root).as_posix()
        content = path.read_bytes()
        if relative.endswith('.sh'):
            assert b'\r\n' not in content, 'Shell scripts must use LF'
        name = f'{base}/{relative}'
        entry = tarfile.TarInfo(name)
        entry.size, entry.mode = len(content), 0o755 if relative.endswith('.sh') else 0o644
        tar.addfile(entry,io.BytesIO(content))
        archive.writestr(name,content)
checks=[]
for extension in ('tar.gz','zip'):
    path=dist/f'{base}.{extension}'
    checks.append(hashlib.sha256(path.read_bytes()).hexdigest()+'  '+path.name)
    print(path.name, path.stat().st_size, 'bytes')
(dist/'SHA256SUMS').write_text('\n'.join(checks)+'\n',encoding='ascii')
with tarfile.open(dist/f'{base}.tar.gz') as tar:
    names=tar.getnames()
    assert len(names)==len(files)
    assert not any('/data/' in n or n.endswith('/config.toml') or n.endswith('/telegram.env') for n in names)
    assert f'{base}/fxwatch/static/app.js' in names
    assert f'{base}/deploy/config.docker.toml' in names
    assert f'{base}/Dockerfile' in names
    assert f'{base}/compose.yaml' in names
    assert f'{base}/fxwatch/container.py' in names
    assert f'{base}/scripts/publish-image.sh' in names
    assert not any(n.endswith('/.env') for n in names)
    for entry in tar:
        assert tar.extractfile(entry).read() == (root/Path(entry.name).relative_to(base)).read_bytes()
    print('Verified archive contents:',len(names),'files; no local config, runtime database or credentials.')
