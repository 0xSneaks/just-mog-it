"""Poll X mentions and reply to eligible mog requests with an image."""

import logging
import os
import re
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import requests
import tweepy
from google import genai

from .detector import detect_intent
from .pfp import render_pfp

SINCE_ID_PATH = Path("tmp/since_id.txt")
POLL_INTERVAL_SECONDS = 60
AUTONOMOUS_POSTING_DISABLED = ("mogit.bot autonomous posting is disabled: every reply needs per-item owner "
                               "approval. Use the approval queue: python3 -m bot.run mogme-preview")
LOGGER = logging.getLogger(__name__)


class MogBot:
    def __init__(self) -> None:
        api_key = os.environ["X_API_KEY"]
        api_secret = os.environ["X_API_SECRET"]
        access_token = os.environ["X_ACCESS_TOKEN"]
        access_secret = os.environ["X_ACCESS_SECRET"]

        auth = tweepy.OAuth1UserHandler(api_key, api_secret, access_token, access_secret)
        self.api_v1 = tweepy.API(auth, timeout=30)
        self.client = tweepy.Client(
            bearer_token=os.environ["X_BEARER_TOKEN"],
            consumer_key=api_key,
            consumer_secret=api_secret,
            access_token=access_token,
            access_token_secret=access_secret,
        )
        self.gemini = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    def _mentions(self, bot_id: int, since_id: int | None) -> list:
        mentions = []
        pages = tweepy.Paginator(
            self.client.get_users_mentions,
            id=bot_id,
            since_id=since_id,
            expansions=["author_id"],
            user_fields=["verified", "public_metrics", "profile_image_url"],
            max_results=100,
        )
        for page in pages:
            authors = {
                author.id: author
                for author in (page.includes or {}).get("users", [])
            }
            mentions.extend(
                (mention, authors.get(mention.author_id)) for mention in (page.data or [])
            )
        return sorted(mentions, key=lambda item: item[0].id)

    def _reply(self, mention: tweepy.Tweet, author: tweepy.User | None) -> None:
        metrics = getattr(author, "public_metrics", None)
        if isinstance(metrics, dict):
            metrics = SimpleNamespace(**metrics)
        if getattr(author, "verified", False) is not True:
            return
        if getattr(metrics, "following_count", 0) <= 60:
            return
        avatar_url = getattr(author, "profile_image_url", None)
        if not avatar_url:
            return

        avatar_url = re.sub(r"_normal(?=\.[^./?]+(?:\?|$))", "_400x400", avatar_url)
        with TemporaryDirectory(dir="tmp") as temp_dir:
            pfp_path = Path(temp_dir) / "pfp.jpg"
            output_path = Path(temp_dir) / "mogged.png"
            response = requests.get(avatar_url, timeout=15)
            response.raise_for_status()
            pfp_path.write_bytes(response.content)

            intent = detect_intent(self.gemini, mention.text, pfp_path)
            if intent.action != "mog" or intent.confidence < 0.6:
                return

            render_pfp(pfp_path, intent, output_path)
            media = self.api_v1.media_upload(filename=str(output_path))
            self.client.create_tweet(
                text="Get mogged!",
                media_ids=[media.media_id],
                in_reply_to_tweet_id=mention.id,
            )

    def listen(self) -> None:
        # Owner gate (2026-11-07): no autonomous X posting. X written approval for AI replies and
        # owner go-live are not granted, so every reply needs per-item owner approval. "mog me"
        # runs through the approval queue instead: python3 -m bot.run mogme-preview / mogme-approve.
        raise SystemExit(AUTONOMOUS_POSTING_DISABLED)
        os.makedirs("tmp", exist_ok=True)
        try:
            stored_id = SINCE_ID_PATH.read_text().strip()
        except FileNotFoundError:
            stored_id = ""
        since_id = int(stored_id) if stored_id else None
        bot_id = self.client.get_me().data.id

        while True:
            try:
                mentions = self._mentions(bot_id, since_id)
            except tweepy.TweepyException:
                LOGGER.exception("Could not poll X mentions")
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            for mention, author in mentions:
                self._reply(mention, author)
                since_id = mention.id
                pending = SINCE_ID_PATH.with_suffix(".tmp")
                pending.write_text(str(since_id))
                pending.replace(SINCE_ID_PATH)
            time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    MogBot().listen()
