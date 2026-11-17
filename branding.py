"""Render the web mark from the same geometry as the native app icon."""
import html
import json
from pathlib import Path


def icon_svg():
    brand=json.loads((Path(__file__).resolve().parent/'app/brand.json').read_text())
    size=brand['canvas'];inset=brand['inset']
    points=' '.join(f'{x},{y}' for x,y in brand['points'])
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}">'
            f'<rect x="{inset}" y="{inset}" width="{size-2*inset}" height="{size-2*inset}" rx="{brand["radius"]}" fill="{html.escape(brand["background"],quote=True)}"/>'
            f'<polyline points="{points}" fill="none" stroke="{html.escape(brand["foreground"],quote=True)}" stroke-width="{brand["stroke"]}" stroke-linecap="round" stroke-linejoin="round"/></svg>')
