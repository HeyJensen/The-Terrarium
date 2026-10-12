"""Terrarium voice: Whisper listens, Fish Audio speaks.

Runs on your own computer next to the Terrarium console. The website (local
or the public GitHub Pages copy) finds it at http://127.0.0.1:8788 and uses it
for the mic and for reading answers aloud. When it isn't running, the site
falls back to the browser's own free voice, so nothing breaks.

  pip install faster-whisper          # once; free, runs on your machine
  set FISH_AUDIO_API_KEY=...          # Windows (export ... on Mac/Linux); paid per use
  python website/voice_server.py

Environment (secrets only live here, never in code):
  FISH_AUDIO_API_KEY   required for speaking; without it the site uses the browser voice
  FISH_AUDIO_VOICE_ID  optional voice model id from fish.audio (default: their default voice)
  FISH_AUDIO_MODEL     optional TTS model header, e.g. s2.1-pro (default: Fish Audio's default)
  WHISPER_MODEL        optional Whisper size: tiny.en, base.en (default), small.en, ...
  VOICE_PORT           optional port (default 8788)

Agents can use the same two calls directly:
  from website.voice_server import transcribe, speak
  text = transcribe("question.webm"); mp3 = speak("Good morning, Nathan.")

Endpoints:
  GET  /api/voice/status  -> {"listen": bool, "speak": bool, ...}
  POST /api/voice/listen  raw audio body (webm/ogg/wav/mp3) -> {"text": "..."}
  POST /api/voice/speak   {"text": "..."} -> audio/mpeg
"""
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FISH_TTS_URL = "https://api.fish.audio/v1/tts"
MAX_AUDIO_BYTES = 10 * 1024 * 1024
MAX_SPEAK_CHARS = 2000
# Pages allowed to use this server from the browser.
ALLOWED_ORIGINS = {"https://heyjensen.github.io", "null"}  # "null" = the page opened as a local file
LOCAL_PREFIXES = ("http://localhost", "http://127.0.0.1")

_whisper = None
_whisper_lock = threading.Lock()


def whisper_available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def _model():
    global _whisper
    with _whisper_lock:
        if _whisper is None:
            from faster_whisper import WhisperModel
            # int8 on CPU keeps it small and quick enough for short questions.
            _whisper = WhisperModel(os.environ.get("WHISPER_MODEL", "base.en"), device="auto", compute_type="int8")
        return _whisper


def transcribe(path: str) -> str:
    """Speech in an audio file -> text, using Whisper on this machine."""
    segments, _ = _model().transcribe(path, language="en", vad_filter=True, beam_size=1)
    return " ".join(s.text.strip() for s in segments).strip()


def speak(text: str) -> bytes:
    """Text -> MP3 bytes, using Fish Audio. Needs FISH_AUDIO_API_KEY."""
    key = os.environ.get("FISH_AUDIO_API_KEY")
    if not key:
        raise RuntimeError("FISH_AUDIO_API_KEY is not set")
    body = {"text": text[:MAX_SPEAK_CHARS], "format": "mp3", "latency": "normal"}
    if os.environ.get("FISH_AUDIO_VOICE_ID"):
        body["reference_id"] = os.environ["FISH_AUDIO_VOICE_ID"]
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if os.environ.get("FISH_AUDIO_MODEL"):
        headers["model"] = os.environ["FISH_AUDIO_MODEL"]
    req = urllib.request.Request(FISH_TTS_URL, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        reason = {401: "the API key was refused", 402: "the Fish Audio account is out of credit"}.get(e.code, f"HTTP {e.code}")
        raise RuntimeError(f"Fish Audio: {reason}") from e


def status() -> dict:
    return {"listen": whisper_available(), "speak": bool(os.environ.get("FISH_AUDIO_API_KEY")),
            "whisper_model": os.environ.get("WHISPER_MODEL", "base.en"),
            "voice_id": os.environ.get("FISH_AUDIO_VOICE_ID") or "default"}


AUDIO_SUFFIX = {"audio/webm": ".webm", "audio/ogg": ".ogg", "audio/wav": ".wav", "audio/x-wav": ".wav",
                "audio/mpeg": ".mp3", "audio/mp4": ".m4a"}


class Handler(BaseHTTPRequestHandler):
    server_version = "TerrariumVoice/1"

    def _origin_ok(self) -> str | None:
        o = self.headers.get("Origin")
        if o is None:
            return None
        return o if (o in ALLOWED_ORIGINS or o.startswith(LOCAL_PREFIXES)) else ""

    def _cors(self):
        o = self._origin_ok()
        if o:
            self.send_header("Access-Control-Allow-Origin", o)
            self.send_header("Vary", "Origin")

    def _json(self, code: int, obj: dict):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        if self._origin_ok() == "":
            self.send_response(403); self.end_headers(); return
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")  # Chrome: a public page calling this computer
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_GET(self):
        if self.path.split("?")[0] == "/api/voice/status":
            return self._json(200, status())
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if self._origin_ok() == "":
            return self._json(403, {"error": "origin not allowed"})
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_AUDIO_BYTES:
            return self._json(413 if length else 400, {"error": "body missing or too large"})
        body = self.rfile.read(length)
        path = self.path.split("?")[0]
        try:
            if path == "/api/voice/listen":
                if not whisper_available():
                    return self._json(503, {"error": "Whisper isn't installed: pip install faster-whisper"})
                ctype = (self.headers.get("Content-Type") or "audio/webm").split(";")[0].strip()
                with tempfile.NamedTemporaryFile(suffix=AUDIO_SUFFIX.get(ctype, ".webm"), delete=False) as f:
                    f.write(body)
                try:
                    return self._json(200, {"text": transcribe(f.name)})
                finally:
                    os.unlink(f.name)
            if path == "/api/voice/speak":
                text = str(json.loads(body).get("text", "")).strip()
                if not text:
                    return self._json(400, {"error": "no text"})
                audio = speak(text)
                self.send_response(200)
                self._cors()
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Content-Length", str(len(audio)))
                self.end_headers()
                self.wfile.write(audio)
                return
        except Exception as e:  # report, don't crash the server
            return self._json(502, {"error": str(e)})
        self._json(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        sys.stderr.write("voice: " + (fmt % args) + "\n")


def main() -> int:
    port = int(os.environ.get("VOICE_PORT", "8788"))
    s = status()
    print(f"Terrarium voice on http://127.0.0.1:{port}")
    print(f"  listening (Whisper {s['whisper_model']}): {'ready' if s['listen'] else 'off, run: pip install faster-whisper'}")
    print(f"  speaking (Fish Audio): {'ready' if s['speak'] else 'off, set FISH_AUDIO_API_KEY'}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
