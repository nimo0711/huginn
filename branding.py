"""Serve the same Forkrail artwork used to package the native app icon."""
import base64
import json
from pathlib import Path


def icon_png():
    root = Path(__file__).resolve().parent
    brand = json.loads((root / 'app/brand.json').read_text())
    return (root / brand['web_image']).read_bytes()


def icon_svg():
    # Keep existing browser tabs' icon URL working across the artwork update.
    data = base64.b64encode(icon_png()).decode('ascii')
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
            f'<image width="64" height="64" href="data:image/png;base64,{data}"/></svg>')
