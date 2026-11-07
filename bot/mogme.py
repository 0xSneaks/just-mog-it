"""'mog me' renderer: put OUR wraparound shield shades on a PFP. Original art drawn in code
(no Pit Viper logo or product photo). Only the glasses are added; nothing else on the face
is altered. Output: square 1000x1000 PNG, EXIF stripped."""
import io
import math

from PIL import Image, ImageDraw, ImageFilter

OUT = 1000
LENS = [(255, 46, 147), (255, 140, 0), (255, 221, 0), (0, 214, 201)]   # pink→orange→yellow→teal

def _gradient(w, h):
    g = Image.new("RGBA", (w, h))
    px = g.load()
    stops = len(LENS) - 1
    for x in range(w):
        t = x / max(w - 1, 1) * stops
        i = min(int(t), stops - 1)
        f = t - i
        c = tuple(int(LENS[i][k] + (LENS[i + 1][k] - LENS[i][k]) * f) for k in range(3))
        for y in range(h):
            px[x, y] = (*c, 235)
    return g

def glasses(width):
    """Shield shades sized so the lens spans `width` px. Returns RGBA."""
    w, h = int(width), int(width * 0.36)
    pad = int(w * 0.06)
    img = Image.new("RGBA", (w + 2 * pad, h + 2 * pad), (0, 0, 0, 0))
    lens = [(pad, pad + h * 0.12), (pad + w, pad + h * 0.12), (pad + w * 0.96, pad + h * 0.78),
            (pad + w * 0.62, pad + h), (pad + w * 0.5, pad + h * 0.80), (pad + w * 0.38, pad + h),
            (pad + w * 0.04, pad + h * 0.78)]
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).polygon(lens, fill=255)
    img.paste(_gradient(*img.size), (0, 0), mask)
    d = ImageDraw.Draw(img)
    stroke = max(3, w // 40)
    d.line(lens + [lens[0]], fill=(255, 255, 255, 255), width=stroke, joint="curve")
    d.rectangle([pad - stroke, pad, pad + w + stroke, pad + h * 0.16], fill=(255, 255, 255, 255))
    shine = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(shine).polygon([(pad + w * 0.12, pad + h * 0.28), (pad + w * 0.24, pad + h * 0.28),
                                   (pad + w * 0.14, pad + h * 0.66), (pad + w * 0.06, pad + h * 0.66)],
                                  fill=(255, 255, 255, 110))
    return Image.alpha_composite(img, shine.filter(ImageFilter.GaussianBlur(stroke / 2)))

def _square(im):
    im = im.convert("RGBA")
    s = min(im.size)
    left, top = (im.width - s) // 2, (im.height - s) // 2
    return im.crop((left, top, left + s, top + s)).resize((OUT, OUT), Image.LANCZOS), s, left, top

def render(pfp_bytes, eyes):
    """eyes = ((lx, ly), (rx, ry)) in source-image pixels. Returns PNG bytes."""
    src = Image.open(io.BytesIO(pfp_bytes))
    src.load()
    im, s, left, top = _square(src)
    k = OUT / s
    (lx, ly), (rx, ry) = [((x - left) * k, (y - top) * k) for x, y in eyes]
    if rx < lx:
        (lx, ly), (rx, ry) = (rx, ry), (lx, ly)
    dist = math.hypot(rx - lx, ry - ly)
    if dist < 8:
        raise ValueError("eyes too close together to place glasses")
    angle = math.degrees(math.atan2(ry - ly, rx - lx))
    g = glasses(dist * 2.3).rotate(-angle, resample=Image.BICUBIC, expand=True)
    cx, cy = (lx + rx) / 2, (ly + ry) / 2
    layer = Image.new("RGBA", im.size, (0, 0, 0, 0))
    layer.paste(g, (int(cx - g.width / 2), int(cy - g.height * 0.45)), g)   # clips at edges
    im = Image.alpha_composite(im, layer)
    out = io.BytesIO()
    im.convert("RGB").save(out, "PNG", optimize=True)      # re-encode drops EXIF
    data = out.getvalue()
    if len(data) > 5_000_000:
        raise ValueError("render over 5 MB")
    return data
