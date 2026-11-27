#!/usr/bin/env python3
"""Check the skill's bundled runtime files without reading any profiling data."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def verify(check_hashes=False):
    manifest = json.loads((ROOT/'references/source_manifest.json').read_text(encoding='utf-8'))
    errors = []
    for row in manifest['files']:
        path = ROOT/row['packaged']
        if not path.is_file():
            errors.append('缺少文件：'+row['packaged'])
        elif check_hashes:
            content = path.read_bytes()
            matches = hashlib.sha256(content).hexdigest() == row['sha256']
            if not matches and path.suffix in {'.py', '.js', '.html', '.txt'}:
                # Git may check out the canonical LF files as CRLF on Windows.
                matches = hashlib.sha256(content.replace(b'\r\n', b'\n')).hexdigest() == row['sha256']
            if not matches:
                errors.append('内容与发布清单不同：'+row['packaged'])
    if sys.version_info < (3, 10):
        errors.append('需要Python 3.10或以上')
    if importlib.util.find_spec('ijson') is None:
        errors.append('缺少ijson；执行 python3 -m pip install -r '+str(Path(__file__).resolve().parent/'requirements.txt'))
    return {'passed':not errors,'files_checked':len(manifest['files']),'hashes_checked':check_hashes,'errors':errors}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--check-hashes',action='store_true')
    args=p.parse_args();result=verify(args.check_hashes)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result['passed'] else 2


if __name__=='__main__':
    raise SystemExit(main())
