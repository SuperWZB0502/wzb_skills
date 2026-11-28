"""Extract the pinned MindStudio Insight timeline marker assets."""
import argparse
import json
from pathlib import Path
import re
import subprocess


REVISION = '2cd7b16e2d77d7f2bf15ae7e1bee0f112f108e79'
MARKER = 'modules/timeline/src/components/TimelineMarker.tsx'
PALETTE = 'modules/timeline/src/entity/timeMaker.tsx'
OUTPUT = Path(__file__).with_name('mindstudio_timeline.json')


def extract(repo):
    def source(path):
        return subprocess.run(['git', '-C', str(repo), 'show', f'{REVISION}:{path}'],
                              check=True, capture_output=True, encoding='utf-8').stdout

    marker = re.search(r'const FLAG_PATH\s*=\s*new Path2D\((.*?)\);', source(MARKER), re.S)
    colors = re.search(r'timelineFlagColorList:\s*\[([^\]]+)\]', source(PALETTE))
    if not marker or not colors:
        raise ValueError('Pinned MindStudio marker definitions not found')
    return {
        'commit': REVISION, 'sources': [MARKER, PALETTE],
        'copyright': 'Copyright (c) 2025 Huawei Technologies Co.,Ltd.',
        'license': 'MulanPSL-2.0',
        'license_text': source('License'),
        'flag_path': ''.join(re.findall(r"'([^']+)'", marker[1])),
        'flag_colors': re.findall(r"'(#[0-9A-Fa-f]{6})'", colors[1]),
    }


if __name__ == '__main__':
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('repository', type=Path)
    cli.add_argument('--check', action='store_true')
    args = cli.parse_args()
    data = extract(args.repository)
    if args.check:
        if json.loads(OUTPUT.read_text(encoding='utf-8')) != data:
            cli.error('packaged marker assets differ from the pinned revision')
    else:
        OUTPUT.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8', newline='\n')
