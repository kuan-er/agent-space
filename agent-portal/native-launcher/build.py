import hashlib,json,os,subprocess,sys
from pathlib import Path
root=Path(__file__).resolve().parent
out=Path(sys.argv[1]);out.mkdir(parents=True,exist_ok=True)
go=os.environ.get('GO', 'go')
manifest={}
for arch in ['amd64','arm64']:
 p=out/('harness-launcher-windows-'+arch+'.exe')
 subprocess.run([str(go),'build','-trimpath','-ldflags=-s -w','-o',str(p),str(root/'main.go')],env={**os.environ,'GOOS':'windows','GOARCH':arch,'CGO_ENABLED':'0'},check=True)
 manifest[arch]=hashlib.sha256(p.read_bytes()).hexdigest()
(out/'hashes.json').write_text(json.dumps(manifest,indent=2));print(json.dumps(manifest))
