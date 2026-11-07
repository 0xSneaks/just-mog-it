"""Gemini Vision: image inspection gate (spec §4) and eye landmarks for "mog me".
Env: JMI_GEMINI_KEY, JMI_GEMINI_MODEL (set the model name explicitly; names change).
Strict JSON out; anything unparseable is treated as a failed inspection (reject)."""
import base64
import io
import json

from PIL import Image

from .http import Http, env

BASE = "https://generativelanguage.googleapis.com/v1beta/models/"

INSPECT_PROMPT = ("Return ONLY JSON: {\"is_real_person\": bool, \"is_brand_logo\": bool, "
                  "\"nsfw_score\": number 0-1, \"text_in_image\": string, \"confidence\": number 0-1}. "
                  "is_real_person = a photo of an identifiable real human. Cartoons/memes/animals are false.")
EYES_PROMPT = ("Image is {w}x{h} px. Return ONLY JSON {{\"left_eye\": [x, y], \"right_eye\": [x, y]}} with the "
               "pixel centers of the main character's eyes (human, animal or cartoon). If no single clear "
               "face, return {{\"left_eye\": null, \"right_eye\": null}}.")

class Gemini:
    def __init__(self, http=None, key=None, model=None):
        self.http, self._key, self._model = http or Http(timeout=20), key, model

    def _ask(self, img, prompt):
        fmt = (Image.open(io.BytesIO(img)).format or "PNG").lower()
        mime = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp"}.get(fmt, "image/png")
        r = self.http.json("POST", f"{BASE}{self._model or env('JMI_GEMINI_MODEL')}:generateContent",
                           headers={"x-goog-api-key": self._key or env("JMI_GEMINI_KEY")},
                           json_body={"contents": [{"parts": [
                               {"inline_data": {"mime_type": mime, "data": base64.b64encode(img).decode()}},
                               {"text": prompt}]}],
                               "generationConfig": {"responseMimeType": "application/json", "temperature": 0}})
        return json.loads(r["candidates"][0]["content"]["parts"][0]["text"])

    def inspect(self, img):
        try:
            out = self._ask(img, INSPECT_PROMPT)
        except Exception:
            return {"confidence": 0, "nsfw_score": 1.0, "error": "inspection_failed"}   # fail closed
        if float(out.get("confidence", 0)) < 0.85:
            out["nsfw_score"] = max(float(out.get("nsfw_score", 0)), 1.0)                 # low confidence = reject
        return out

    def eyes(self, img):
        w, h = Image.open(io.BytesIO(img)).size
        try:
            out = self._ask(img, EYES_PROMPT.format(w=w, h=h))
            l, r = out.get("left_eye"), out.get("right_eye")
        except Exception:
            return None
        if not (l and r) or not all(0 <= v[0] <= w and 0 <= v[1] <= h for v in (l, r)):
            return None
        return (tuple(l), tuple(r))
