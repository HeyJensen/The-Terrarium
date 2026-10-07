"""Publishing approved social posts through Buffer (free plan includes the API).

Buffer holds the Facebook Page (and later other channels) connection, so the
organism never touches a social login. Posts go into Buffer's queue, which
publishes at the slots set in Buffer.

API: GraphQL at https://api.buffer.com, "Authorization: Bearer $BUFFER_API_KEY".
"""
import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API_URL = "https://api.buffer.com"

# Text that means a human still has to fill something in.
UNFINISHED_MARKERS = ("[EDIT", "[[")


class BufferError(RuntimeError):
    pass


class BufferClient:
    def __init__(self, api_key: str, url: str = API_URL, timeout: int = 20):
        self.api_key, self.url, self.timeout = api_key, url, timeout

    def _gql(self, query: str, variables: dict | None = None) -> dict:
        req = urllib.request.Request(
            self.url, data=json.dumps({"query": query, "variables": variables or {}}).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"}, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            body = json.loads(r.read())
        if body.get("errors"):
            raise BufferError("; ".join(e.get("message", "?") for e in body["errors"]))
        return body["data"]

    def channels(self) -> list[dict]:
        """Every connected channel across the account's organizations: [{id, name, service}]."""
        orgs = self._gql("query { account { organizations { id } } }")["account"]["organizations"]
        out = []
        for org in orgs:
            # Values are inlined as JSON string literals (valid GraphQL strings), so no
            # schema type names have to be guessed for variables.
            out += self._gql(f"query {{ channels(input: {{organizationId: {json.dumps(org['id'])}}}) "
                             "{ id name service } }")["channels"]
        return out

    def queue_post(self, channel_id: str, text: str) -> dict:
        data = self._gql(
            f"mutation {{ createPost(input: {{text: {json.dumps(text, ensure_ascii=False)}, channelId: {json.dumps(channel_id)}, "
            "schedulingType: automatic, mode: addToQueue}) { ... on PostActionSuccess { post { id dueAt } } "
            "... on MutationError { message } } }")["createPost"]
        if "post" not in data:
            raise BufferError(data.get("message", "createPost failed"))
        return data["post"]


def post_text(draft: dict) -> str:
    text = draft["text"].strip()
    link = (draft.get("link") or "").strip()
    return f"{text}\n\n{link}" if link and link not in text else text


def unfinished(text: str) -> bool:
    return any(m in text for m in UNFINISHED_MARKERS)


def publish(cfg, queue, decisions, client, state_dir: Path, dry_run: bool, now: datetime | None = None) -> list[dict]:
    """Queue approved, finished social posts in Buffer, at most daily_post_limit per UTC day.

    Returns one result per post considered: {id, platform, text, outcome}.
    """
    now = now or datetime.now(timezone.utc)
    state_dir.mkdir(parents=True, exist_ok=True)
    log_path = state_dir / "published.json"
    published = json.loads(log_path.read_text()) if log_path.exists() else {}
    today = now.date().isoformat()
    sent_today = sum(1 for p in published.values() if p["at"].startswith(today))

    channels = {}
    if not dry_run:
        for c in client.channels():
            channels.setdefault(c["service"].lower(), c["id"])

    results = []
    for item in queue.list(status="approved", kind="social_post"):
        if item["id"] in published:
            continue
        d = item["draft"]
        text = post_text(d)
        res = {"id": item["id"], "platform": d["platform"], "text": text}
        if d["platform"] not in cfg["platforms"]:
            res["outcome"] = "skipped: platform not enabled"
        elif unfinished(text):
            res["outcome"] = "skipped: still has [EDIT] placeholders; approve it again with --text and --link"
        elif sent_today >= cfg["daily_post_limit"]:
            res["outcome"] = f"held: daily limit of {cfg['daily_post_limit']} reached"
        elif dry_run:
            res["outcome"] = "would queue in Buffer (dry run)"
            sent_today += 1
        elif d["platform"] not in channels:
            res["outcome"] = f"skipped: no {d['platform']} channel connected in Buffer"
        else:
            post = client.queue_post(channels[d["platform"]], text)
            published[item["id"]] = {"at": now.isoformat(timespec="seconds"), "buffer_post_id": post["id"],
                                     "due_at": post.get("dueAt")}
            log_path.write_text(json.dumps(published, indent=2))
            res["outcome"] = f"queued in Buffer, due {post.get('dueAt')}"
            sent_today += 1
        decisions.record("publish_social", res["outcome"], approval_id=item["id"], platform=d["platform"])
        results.append(res)
    return results
