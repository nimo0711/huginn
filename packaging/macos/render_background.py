"""Render the Finder background specified in design/DESIGN.md (macOS only)."""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess


def render_background(output: Path) -> Path:
    from PIL import Image, ImageDraw, ImageFont

    output.mkdir(parents=True, exist_ok=True)
    for scale in (1, 2):
        canvas = Image.new('RGB', (720 * scale, 440 * scale), '#ece5d5')
        draw = ImageDraw.Draw(canvas)

        def text(label, top, size, color, *, korean=False, weight='Regular'):
            name = 'AppleSDGothicNeo.ttc' if korean else 'SFNS.ttf'
            font = ImageFont.truetype('/System/Library/Fonts/' + name, size * scale)
            if not korean:
                font.set_variation_by_name(weight)
            draw.text((360 * scale, top * scale), label, font=font, fill=color, anchor='mt')

        draw.line([(328 * scale, 40 * scale), (356 * scale, 40 * scale)], fill='#24221d', width=4 * scale)
        draw.line([(364 * scale, 40 * scale), (392 * scale, 40 * scale)], fill='#a88b4f', width=4 * scale)
        text('Huginn', 60, 32, '#24221d', weight='Semibold')
        text('Drag to Applications to install.', 112, 16, '#24221d')
        text('Huginn을 응용 프로그램 폴더로 드래그하세요.', 140, 14, '#625b4e', korean=True)
        draw.line([(332 * scale, 244 * scale), (388 * scale, 244 * scale)], fill='#24221d', width=3 * scale)
        draw.line([(376 * scale, 232 * scale), (388 * scale, 244 * scale), (376 * scale, 256 * scale)],
                  fill='#24221d', width=3 * scale, joint='curve')
        text('복사가 끝나면 응용 프로그램에서 Huginn을 실행하세요.', 388, 12, '#625b4e', korean=True)
        canvas.save(output / f'background@{scale}x.png', dpi=(72 * scale, 72 * scale))
    target = output / 'background.tiff'
    subprocess.run(['tiffutil', '-cathidpicheck', str(output / 'background@1x.png'),
                    str(output / 'background@2x.png'), '-out', str(target)], check=True)
    return target


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    render_background(parser.parse_args().output)
