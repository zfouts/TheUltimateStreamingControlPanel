"""The control panel's HTTP API: a tiny router shared by the API modules.

Each API module exposes `register(router, editor)` and adds its routes:

    def register(router, ed):
        @router.get("/api/live/deck")
        def deck(query):            # query: {name: first value} from the URL
            return {...}            # any JSON-serialisable dict

        @router.post("/api/live/page")
        def page(body):             # body: the parsed JSON object
            return {"ok": True}

        @router.upload("/api/setup/upload", max_bytes=50 << 20, max_seconds=120)
        def upload(stream):         # raw application/octet-stream body, ≤ max_bytes, ≤ max_seconds
            data = stream.read(1 << 20)   # stream.length: the Content-Length; read() until b""
            return {...}

Every other POST takes a JSON body of at most server.MAX_BODY.

Raise `ApiError(message, status=400)` for a client error; any other exception becomes a
400 with its message. The server (server.py) does the access checks before routing.
"""
import re


class ApiError(Exception):
    def __init__(self, message, status=400, **extra):
        super().__init__(message)
        self.status = status
        self.extra = extra   # more JSON fields for the error body


class Router:
    def __init__(self):
        self.gets, self.posts = {}, {}
        self.get_patterns, self.post_patterns = [], []   # (compiled regex, fn(match, query|body))
        self.uploads = {}   # path -> (fn, max_bytes, max_seconds): raw-body routes with their own limits

    def get(self, path):
        def deco(fn):
            self.gets[path] = fn
            return fn
        return deco

    def post(self, path):
        def deco(fn):
            self.posts[path] = fn
            return fn
        return deco

    def upload(self, path, max_bytes, max_seconds=None):
        def deco(fn):
            self.uploads[path] = (fn, int(max_bytes), max_seconds)
            return fn
        return deco

    def get_match(self, pattern):
        """A GET route for every path fully matching `pattern`: fn(match, query)."""
        def deco(fn):
            self.get_patterns.append((re.compile(pattern), fn))
            return fn
        return deco

    def post_match(self, pattern):
        """A POST route for every path fully matching `pattern`: fn(match, body)."""
        def deco(fn):
            self.post_patterns.append((re.compile(pattern), fn))
            return fn
        return deco

    @staticmethod
    def _find(exact, patterns, path):
        fn = exact.get(path)
        if fn:
            return fn
        for rx, pfn in patterns:
            m = rx.fullmatch(path)
            if m:
                return lambda arg, _m=m, _f=pfn: _f(_m, arg)
        return None

    def find_get(self, path):
        """The handler for a GET path (exact routes first, then patterns), or None."""
        return self._find(self.gets, self.get_patterns, path)

    def find_post(self, path):
        return self._find(self.posts, self.post_patterns, path)

    def add_get(self, path, fn):
        self.gets[path] = fn

    def add_post(self, path, fn):
        self.posts[path] = fn
