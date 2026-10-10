"""Brand images: HA 2026.3+ serves custom_components/<domain>/brand/*.png before the brands CDN.

The file names are the eight in homeassistant/components/brands/const.py ALLOWED_IMAGES; anything else
is never requested. Sizes follow home-assistant/brands: icons square 256 / @2x 512, logos landscape with
the short side 128 / @2x 256, both trimmed and on a transparent background. The PNG files are committed as they are.
"""

from pathlib import Path

import pytest
from PIL import Image

BRAND = Path(__file__).resolve().parents[1] / "custom_components" / "jma" / "brand"
ICONS = {"icon.png": 256, "icon@2x.png": 512, "dark_icon.png": 256, "dark_icon@2x.png": 512}
LOGOS = {"logo.png": 128, "logo@2x.png": 256, "dark_logo.png": 128, "dark_logo@2x.png": 256}


def _open(name: str) -> Image.Image:
    image = Image.open(BRAND / name)
    assert image.format == "PNG" and image.mode == "RGBA", (name, image.format, image.mode)
    return image


def test_only_the_names_home_assistant_asks_for():
    assert {p.name for p in BRAND.iterdir()} == set(ICONS) | set(LOGOS)


@pytest.mark.parametrize("name", sorted(set(ICONS) | set(LOGOS)))
def test_nothing_is_cut_off_at_the_edge(name):
    # art cropped by the trim leaves fully opaque pixels on the border (for example round stroke caps
    # cut flat because getBBox ignores stroke width)
    alpha = _open(name).getchannel("A")
    width, height = alpha.size
    border = [alpha.getpixel((x, y)) for x in range(width) for y in (0, height - 1)]
    border += [alpha.getpixel((x, y)) for y in range(height) for x in (0, width - 1)]
    assert max(border) < 255, (name, max(border))


@pytest.mark.parametrize("name", ["icon.png", "logo.png"])
def test_dark_variant_is_really_different(name):
    assert (BRAND / name).read_bytes() != (BRAND / f"dark_{name}").read_bytes()


@pytest.mark.parametrize(("name", "size"), ICONS.items())
def test_icon_is_square_trimmed_and_transparent(name, size):
    image = _open(name)
    assert image.size == (size, size)
    corners = [image.getpixel(p)[3] for p in ((0, 0), (size - 1, 0), (0, size - 1), (size - 1, size - 1))]
    assert corners == [0, 0, 0, 0]
    left, top, right, bottom = image.getchannel("A").getbbox()
    # trimmed: the mark is taller than wide, so it spans the full height (2 % anti-alias margin)
    assert top <= size * 0.02 and bottom >= size * 0.98, (top, bottom)
    assert left > 0 and right < size  # centred horizontally, not cropped


@pytest.mark.parametrize(("name", "short"), LOGOS.items())
def test_logo_is_landscape_and_transparent(name, short):
    image = _open(name)
    width, height = image.size
    assert height == short and width > 2 * height, image.size
    assert image.getpixel((0, 0))[3] == 0 and image.getpixel((width - 1, height - 1))[3] == 0
