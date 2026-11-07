"""X API v2 adapter. Auth: OAuth 2.0 USER token issued to @justmogit_bot with scopes
tweet.read tweet.write users.read media.write offline.access (env JMI_X_USER_TOKEN).
Media: POST api.x.com/2/media/upload INIT/APPEND/FINALIZE (multipart), id at data.id."""
import json
import urllib.parse

from .http import Http, env

API = "https://api.x.com/2"
CHUNK = 4 * 1024 * 1024

class XClient:
    def __init__(self, http=None, token=None, bot_user_id=None):
        self.http = http or Http()
        self._token = token
        self.bot_user_id = bot_user_id or env("JMI_X_BOT_USER_ID")

    def _h(self):
        return {"Authorization": f"Bearer {self._token or env('JMI_X_USER_TOKEN')}"}

    def mentions(self, since_id=None):
        """Mentions of the bot, oldest first, with quoted-tweet text and author handle joined in.
        Returns (mentions, newest_id)."""
        q = {"max_results": "100", "expansions": "author_id,referenced_tweets.id",
             "tweet.fields": "author_id,referenced_tweets,created_at", "user.fields": "username,profile_image_url"}
        if since_id:
            q["since_id"] = since_id
        r = self.http.json("GET", f"{API}/users/{self.bot_user_id}/mentions?{urllib.parse.urlencode(q)}",
                           headers=self._h())
        inc = r.get("includes", {})
        users = {u["id"]: u.get("username") for u in inc.get("users", [])}
        avatars = {u["id"]: u.get("profile_image_url") for u in inc.get("users", [])}
        tweets = {t["id"]: t.get("text", "") for t in inc.get("tweets", [])}
        out = []
        for t in reversed(r.get("data", [])):
            if t.get("author_id") == self.bot_user_id:
                continue                                   # never react to ourselves
            quoted = next((x["id"] for x in t.get("referenced_tweets", []) if x["type"] == "quoted"), None)
            refs = {x.get("type") for x in t.get("referenced_tweets", [])}
            out.append({"id": t["id"], "author_id": t["author_id"], "handle": users.get(t["author_id"]),
                        "text": t.get("text", ""), "quoted_tweet_id": quoted,
                        "quoted_text": tweets.get(quoted) if quoted else None,
                        "is_retweet": "retweeted" in refs,
                        "profile_image_url": avatars.get(t["author_id"])})   # the AUTHOR's avatar
        return out, r.get("meta", {}).get("newest_id", since_id)

    def reply(self, tweet_id, text, media_ids=None, idempotency_key=None):
        body = {"text": text, "reply": {"in_reply_to_tweet_id": str(tweet_id)}}
        if media_ids:
            body["media"] = {"media_ids": [str(m) for m in media_ids]}
        r = self.http.json("POST", f"{API}/tweets", headers=self._h(), json_body=body)
        return r["data"]["id"]

    def profile_image_url(self, user_id):
        r = self.http.json("GET", f"{API}/users/{user_id}?user.fields=profile_image_url", headers=self._h())
        return r["data"].get("profile_image_url")

    def profile_image(self, user_id):
        r = self.http.json("GET", f"{API}/users/{user_id}?user.fields=profile_image_url", headers=self._h())
        url = r["data"].get("profile_image_url")
        if not url:
            raise RuntimeError("user has no profile image")
        return self.http.request("GET", url.replace("_normal.", "_400x400."))

    def upload_media(self, png, alt_text=None):
        init = self.http.json("POST", f"{API}/media/upload", headers=self._h(),
                              form={"command": "INIT", "media_type": "image/png",
                                    "total_bytes": str(len(png)), "media_category": "tweet_image"})
        mid = init["data"]["id"]
        for n, i in enumerate(range(0, len(png), CHUNK)):
            self.http.request("POST", f"{API}/media/upload", headers=self._h(), expect=(200, 201, 204),
                              form={"command": "APPEND", "media_id": mid, "segment_index": str(n),
                                    "media": ("pfp.png", png[i:i + CHUNK], "image/png")})
        self.http.json("POST", f"{API}/media/upload", headers=self._h(),
                       form={"command": "FINALIZE", "media_id": mid})
        if alt_text:
            # INFERENCE: v2 alt-text endpoint path; failure here must not block the reply
            try:
                self.http.request("POST", f"{API}/media/metadata", headers=self._h(), expect=(200, 201, 204),
                                  json_body={"id": mid, "metadata": {"alt_text": {"text": alt_text[:1000]}}})
            except Exception:
                pass
        return mid
