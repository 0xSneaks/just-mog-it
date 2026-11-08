"""Head pose for "mog me": the placement of the shades must follow the picture.

read_pose(image)  → Pose: which way the face points (front / three-quarter / profile, left or
                    right), head tilt in degrees, looking up/down, where the eyes are, the art
                    style, and whether the character already wears glasses. One cheap Gemini text
                    call (JSON out). Any failure returns None → the edit gets the generic fit rules.
placement_prompt(pose) → concrete, pose-specific drawing instructions for the shades.
check_fit(edited, pose) → a second cheap call that judges the result: shades over the eyes, same
                    view and tilt as the head, same art style. A fail counts as a failed try.

StubPoseReader (default, IMAGE_EDITOR=stub) makes no network calls."""
import base64
import io
import json
import os
from dataclasses import dataclass

from PIL import Image

VIEWS = ("front", "three_quarter_left", "three_quarter_right", "profile_left", "profile_right", "none")
PITCHES = ("level", "up", "down")
TEXT_MODEL = "gemini-2.5-flash"
TIMEOUT_S = 30
MIN_CONFIDENCE = 0.6
ROLL_NOTE_DEG = 5

POSE_PROMPT = (
    "Look at this profile picture and describe the MAIN character's head (human, animal or cartoon). "
    "Return ONLY JSON: {\"view\": one of \"front\", \"three_quarter_left\", \"three_quarter_right\", "
    "\"profile_left\", \"profile_right\", \"none\" (which way the face points, from the viewer's side; "
    "\"none\" if there is no single clear face), \"roll_deg\": number (head tilt, positive = clockwise), "
    "\"pitch\": \"level\" | \"up\" | \"down\", \"eyes\": list of [x, y] for each VISIBLE eye as fractions "
    "0..1 of width and height, \"art_style\": short phrase (e.g. \"photo\", \"anime illustration\", "
    "\"3D render\", \"pixel art\"), \"wears_glasses\": bool, \"confidence\": number 0..1}.")

CHECK_PROMPT = (
    "This profile picture should show the main character wearing wraparound shield sport sunglasses. "
    "Expected head view: {view}; head tilt about {roll} degrees. Judge ONLY the sunglasses. Return ONLY "
    "JSON: {{\"shades_present\": bool, \"lenses_over_eyes\": bool, \"follows_head_angle\": bool, "
    "\"same_art_style\": bool, \"covers_nose_or_mouth\": bool}}.")


@dataclass(frozen=True)
class Pose:
    view: str
    roll_deg: float
    pitch: str
    eyes: tuple          # ((x, y), ...) fractions 0..1, visible eyes only
    art_style: str
    wears_glasses: bool
    confidence: float

    def asdict(self):
        return {"view": self.view, "roll_deg": self.roll_deg, "pitch": self.pitch,
                "eyes": [list(e) for e in self.eyes], "art_style": self.art_style,
                "wears_glasses": self.wears_glasses, "confidence": self.confidence}


def parse_pose(raw):
    """Validate the model's JSON. Anything off → None (the edit falls back to generic rules)."""
    try:
        view, pitch = raw["view"], raw.get("pitch", "level")
        roll, conf = float(raw.get("roll_deg", 0)), float(raw["confidence"])
        eyes = tuple((float(e[0]), float(e[1])) for e in (raw.get("eyes") or []))
        style = " ".join(str(raw.get("art_style", "")).split())[:40]
        glasses = raw.get("wears_glasses") is True
    except (KeyError, TypeError, ValueError, IndexError):
        return None
    if view not in VIEWS or pitch not in PITCHES or not -90 <= roll <= 90 or not 0 <= conf <= 1:
        return None
    if len(eyes) > 2 or not all(0 <= x <= 1 and 0 <= y <= 1 for x, y in eyes):
        return None
    if view == "none" or conf < MIN_CONFIDENCE:
        return None
    if style and not all(c.isalnum() or c in " -'/" for c in style):
        style = ""
    return Pose(view, round(roll, 1), pitch, eyes, style, glasses, round(conf, 2))


def _side(view):
    return "left" if view.endswith("_left") else "right"


def placement_prompt(pose):
    """Pose-specific drawing instructions. None → '' (the generic fit check still applies)."""
    if pose is None:
        return ""
    out = []
    if pose.view == "front":
        out.append("The face looks straight at the viewer: draw the shades front-on and symmetric, both "
                   "lenses the same size, the bridge centred on the nose, both arms going back behind the ears.")
    elif pose.view.startswith("three_quarter"):
        out.append(f"The head is turned in three-quarter view toward the viewer's {_side(pose.view)}: draw the "
                   "shades in that same three-quarter perspective. The lens on the near side is larger, the far "
                   "lens is foreshortened and partly hidden behind the nose bridge, and only the near arm shows, "
                   "running back to the ear.")
    else:
        out.append(f"The head is in side profile facing the viewer's {_side(pose.view)}: draw the shades from "
                   "the side. One wraparound lens curves around the visible eye, the front edge sits just past "
                   "the brow ridge, the arm runs straight back to the ear, and no second lens is drawn.")
    if abs(pose.roll_deg) >= ROLL_NOTE_DEG:
        way = "clockwise" if pose.roll_deg > 0 else "counter-clockwise"
        out.append(f"The head is tilted about {abs(round(pose.roll_deg))} degrees {way}: tilt the shades by the "
                   "same angle so they stay level with the eyes.")
    if pose.pitch != "level":
        side = "below" if pose.pitch == "up" else "above"
        out.append(f"The head is tilted {pose.pitch}: show the frame from slightly {side} to match.")
    if pose.eyes:
        spots = " and ".join(f"{round(x * 100)}% across, {round(y * 100)}% down" for x, y in pose.eyes)
        out.append(f"The visible eye{'s are' if len(pose.eyes) > 1 else ' is'} at about {spots}: centre the "
                   f"lens{'es' if len(pose.eyes) > 1 else ''} exactly there.")
    if pose.wears_glasses:
        out.append("The character already wears glasses: replace them with the shades, do not stack two pairs.")
    if pose.art_style:
        out.append(f"The picture is a {pose.art_style}: draw the shades as part of that same {pose.art_style}.")
    return " ".join(out)


def fit_ok(raw):
    """True only when every placement answer is right. Unparseable → None (unchecked)."""
    keys = ("shades_present", "lenses_over_eyes", "follows_head_angle", "same_art_style", "covers_nose_or_mouth")
    if not isinstance(raw, dict) or not all(isinstance(raw.get(k), bool) for k in keys):
        return None
    return (raw["shades_present"] and raw["lenses_over_eyes"] and raw["follows_head_angle"]
            and raw["same_art_style"] and not raw["covers_nose_or_mouth"])


class StubPoseReader:
    """No network. Reads no pose (generic fit rules) and never fails a fit check."""
    name = "stub"

    def read_pose(self, image_bytes):
        return None

    def check_fit(self, image_bytes, pose):
        return True


class GeminiPoseReader:
    """Gemini text model with JSON output. Same GEMINI_API_KEY as the image editor. No retry."""
    name = "gemini"

    def __init__(self, api_key, model=TEXT_MODEL, post=None):
        if not api_key or not str(api_key).strip():
            from .mogme_queue import MogMeConfigError
            raise MogMeConfigError("GEMINI_API_KEY missing")
        self._key, self.model = str(api_key).strip(), model
        self._post = post or self._httpx_post

    @staticmethod
    def _httpx_post(url, json, timeout, headers):
        import httpx
        r = httpx.post(url, json=json, timeout=timeout, headers=headers, follow_redirects=False)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {}

    def _ask(self, image_bytes, prompt):
        with Image.open(io.BytesIO(image_bytes)) as im:
            mime = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}.get(im.format, "image/png")
        body = {"contents": [{"parts": [{"inline_data": {"mime_type": mime,
                                                         "data": base64.b64encode(image_bytes).decode()}},
                                        {"text": prompt}]}],
                "generationConfig": {"responseMimeType": "application/json", "temperature": 0}}
        status, out = self._post(f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent",
                                 json=body, timeout=TIMEOUT_S,
                                 headers={"Content-Type": "application/json", "x-goog-api-key": self._key})
        if status != 200:
            raise RuntimeError(f"pose HTTP {status}")
        return json.loads(out["candidates"][0]["content"]["parts"][0]["text"])

    def read_pose(self, image_bytes):
        try:
            return parse_pose(self._ask(image_bytes, POSE_PROMPT))
        except Exception:
            return None

    def check_fit(self, image_bytes, pose):
        view = pose.view.replace("_", " ") if pose else "unknown"
        roll = round(pose.roll_deg) if pose else 0
        try:
            return fit_ok(self._ask(image_bytes, CHECK_PROMPT.format(view=view, roll=roll)))
        except Exception:
            return None


def build_pose_reader(env=None):
    """Follows IMAGE_EDITOR: stub → StubPoseReader, gemini → GeminiPoseReader (needs the key)."""
    env = os.environ if env is None else env
    kind = (env.get("IMAGE_EDITOR") or "stub").strip().lower()
    if kind == "gemini":
        return GeminiPoseReader(env.get("GEMINI_API_KEY", ""),
                                model=(env.get("GEMINI_TEXT_MODEL") or TEXT_MODEL).strip())
    return StubPoseReader()
