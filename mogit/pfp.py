"""Composite the pre-split Pit Viper assets onto a profile picture."""

import os
from pathlib import Path

from PIL import Image, ImageChops, ImageOps

from .detector import MogIntent

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"


def _load_asset(filename: str) -> Image.Image:
    with Image.open(ASSETS_DIR / filename) as asset:
        return asset.convert("RGBA")


def _tint_lens(lens: Image.Image, primary: str, secondary: str) -> Image.Image:
    tinted = ImageOps.colorize(ImageOps.grayscale(lens), primary, secondary)
    tinted.putalpha(lens.getchannel("A"))
    return tinted


def render_pfp(
    image_path: str | Path, intent: MogIntent, output_path: str | Path
) -> None:
    """Save the composited profile picture as a PNG."""
    with Image.open(image_path) as image:
        canvas = image.convert("RGBA")

    frame = _load_asset("pit_vipers_frame.png")
    left = _load_asset("pit_vipers_left_lens.png")
    right = _load_asset("pit_vipers_right_lens.png")
    assert frame.size == left.size == right.size, "Glasses layers need identical canvas sizes"

    tinted_frame = Image.new("RGBA", frame.size, intent.frame_color)
    tinted_frame.putalpha(frame.getchannel("A"))
    glasses = Image.alpha_composite(
        _tint_lens(left, intent.lens_primary_color, intent.lens_secondary_color),
        _tint_lens(right, intent.lens_primary_color, intent.lens_secondary_color),
    )
    glasses = Image.alpha_composite(glasses, tinted_frame)

    scanlines = _load_asset("high_fidelity_scanlines.png").resize(
        glasses.size, Image.Resampling.LANCZOS
    )
    alpha = glasses.getchannel("A")
    mask = ImageChops.multiply(alpha, scanlines.getchannel("A").point(lambda a: a // 5))
    glasses.paste(scanlines.convert("RGB"), (0, 0), mask)
    glasses.putalpha(alpha)

    width = max(1, round(intent.scale_width_px or canvas.width * 0.55))
    height = max(1, round(width * glasses.height / glasses.width))
    glasses = glasses.resize((width, height), Image.Resampling.LANCZOS).rotate(
        intent.rotation_angle_deg, resample=Image.Resampling.BICUBIC, expand=True
    )
    x, y = intent.center_point or (canvas.width / 2, canvas.height / 2)
    canvas.alpha_composite(glasses, (round(x - glasses.width / 2), round(y - glasses.height / 2)))

    stamp = _load_asset("get_mogged_stamp.png")
    stamp_width = max(1, round(canvas.width * 0.30))
    stamp_height = max(1, round(stamp_width * stamp.height / stamp.width))
    stamp = stamp.resize((stamp_width, stamp_height), Image.Resampling.LANCZOS)
    canvas.alpha_composite(stamp, (canvas.width - stamp.width, 0))

    dirname = os.path.dirname(output_path)
    if dirname:
        os.makedirs(dirname, exist_ok=True)
    canvas.save(output_path, format="PNG")
