"""Minimal HTTP client (stdlib). JSON + multipart. 5s default timeout (spec §3)."""
import json
import os
import uuid

class HttpError(RuntimeError):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {body[:300]!r}")
        self.status, self.body = status, body

def env(name):
    v = os.environ.get(name, "").strip()
    if not v:
        raise RuntimeError(f"env {name} not set")
    return v

def urllib_transport(method, url, headers, body, timeout):
    import urllib.error
    import urllib.request                       # lazy: keeps imports socket-free for tests
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()

def multipart(fields):
    """fields: {name: str | (filename, bytes, content_type)}"""
    b = uuid.uuid4().hex
    out = []
    for k, v in fields.items():
        out.append(f"--{b}\r\n".encode())
        if isinstance(v, tuple):
            fn, data, ct = v
            out.append(f'Content-Disposition: form-data; name="{k}"; filename="{fn}"\r\n'
                       f"Content-Type: {ct}\r\n\r\n".encode() + data + b"\r\n")
        else:
            out.append(f'Content-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    out.append(f"--{b}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={b}"

class Http:
    def __init__(self, transport=urllib_transport, timeout=5):
        self.t, self.timeout = transport, timeout

    def request(self, method, url, *, headers=None, json_body=None, form=None, raw=None, expect=(200, 201)):
        h = dict(headers or {})
        body = None
        if json_body is not None:
            body, h["Content-Type"] = json.dumps(json_body).encode(), "application/json"
        elif form is not None:
            body, h["Content-Type"] = multipart(form)
        elif raw is not None:
            body = raw
        status, data = self.t(method, url, h, body, self.timeout)
        if status not in expect:
            raise HttpError(status, data.decode("utf-8", "replace") if isinstance(data, bytes) else str(data))
        return data

    def json(self, method, url, **kw):
        return json.loads(self.request(method, url, **kw))
