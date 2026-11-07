import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

# Allow the pytest console entry point to import the source checkout.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mogit import pfp
from mogit.detector import MogIntent, detect_intent


@pytest.fixture
def mock_pfp(tmp_path):
    path = tmp_path / "profile.jpg"
    Image.new("RGB", (400, 400), (128, 128, 128)).save(path)
    return path


@pytest.fixture
def synthetic_assets(tmp_path, monkeypatch):
    assets_dir = tmp_path / "assets"
    assets_dir.mkdir()
    monkeypatch.setattr(pfp, "ASSETS_DIR", assets_dir)

    rectangles = {
        "pit_vipers_frame.png": (38, 21, 61, 28),
        "pit_vipers_left_lens.png": (1, 5, 47, 44),
        "pit_vipers_right_lens.png": (52, 5, 98, 44),
        "high_fidelity_scanlines.png": (0, 5, 99, 8),
        "get_mogged_stamp.png": (0, 0, 99, 49),
    }
    for filename, bounds in rectangles.items():
        image = Image.new("RGBA", (100, 50))
        ImageDraw.Draw(image).rectangle(bounds, fill="white")
        image.save(assets_dir / filename)


def test_mog_changes_eyes_and_center(mock_pfp, synthetic_assets, tmp_path):
    intent = MogIntent(
        action="mog",
        confidence=0.95,
        left_eye=[150, 200],
        right_eye=[250, 200],
        center_point=[200, 200],
        scale_width_px=200,
    )
    output = tmp_path / "rendered" / "mogged.png"
    pfp.render_pfp(mock_pfp, intent, output)

    with Image.open(mock_pfp) as source, Image.open(output) as result:
        for point in ((150, 200), (250, 200), (200, 200)):
            assert result.getpixel(point)[:3] != source.getpixel(point)


def test_ignore_action_is_preserved(mock_pfp):
    def generate_content(**kwargs):
        assert kwargs["config"].response_schema is MogIntent
        return SimpleNamespace(parsed=None, text='{"action":"ignore","confidence":0.9}')

    client = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    assert detect_intent(client, "Unrelated mention", mock_pfp).action == "ignore"
