"""
voice.py — ElevenLabs text-to-speech proxy.

The ElevenLabs API key lives only on the server (settings.elevenlabs_api_key).
The browser never sees it: it POSTs text + audio options here and gets back an
audio stream. Three endpoints:

  GET  /voice/config → { enabled, voices, models, output_formats, defaults }
                       Everything the sidebar needs to render the voice controls,
                       fetched live from the user's ElevenLabs account (cached 5m).
  POST /voice/tts    → streamed audio bytes (audio/mpeg for mp3). Proxies the
                       ElevenLabs streaming endpoint so playback can start early.
  POST /voice/preview→ same as /tts with a fixed sample sentence (voice preview).

If no key is configured every endpoint degrades gracefully: /config returns
{enabled: false} and /tts returns 503 with a helpful message.
"""
from __future__ import annotations

import logging
import time

import requests
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.settings import get_settings

logger = logging.getLogger("falcon.voice")

router = APIRouter(prefix="/voice", tags=["voice"])

ELEVEN_BASE = "https://api.elevenlabs.io/v1"
PREVIEW_TEXT = "Hello — this is a preview of the selected Falcon voice."

# Output containers ElevenLabs can return. mp3 (and opus) play natively in the
# browser; pcm / µ-law are raw and are labelled as such in the UI.
OUTPUT_FORMATS = [
    "mp3_44100_128",
    "mp3_44100_192",
    "mp3_44100_96",
    "mp3_44100_64",
    "mp3_44100_32",
    "mp3_22050_32",
    "opus_48000_128",
    "opus_48000_64",
    "pcm_16000",
    "pcm_22050",
    "pcm_24000",
    "pcm_44100",
    "ulaw_8000",
]

DEFAULTS = {
    "voice_id": "",  # resolved to the first account voice in /config
    "model_id": "eleven_flash_v2_5",
    "output_format": "mp3_44100_128",
    "stability": 0.5,
    "similarity_boost": 0.75,
    "style": 0.0,
    "use_speaker_boost": True,
    "speed": 1.0,
}

# Simple in-process cache for the (rarely-changing) voice + model catalog so we
# don't hit ElevenLabs on every page load.
_CATALOG_TTL = 300.0
_catalog: dict = {"at": 0.0, "voices": None, "models": None}


def _key() -> str:
    return get_settings().elevenlabs_api_key.strip()


def _headers() -> dict:
    return {"xi-api-key": _key()}


def _media_type(fmt: str) -> str:
    if fmt.startswith("mp3"):
        return "audio/mpeg"
    if fmt.startswith("opus"):
        return "audio/ogg"
    if fmt.startswith("ulaw") or fmt.startswith("alaw"):
        return "audio/basic"
    if fmt.startswith("pcm"):
        return "audio/L16"
    return "application/octet-stream"


def _get_json(path: str) -> dict:
    r = requests.get(f"{ELEVEN_BASE}{path}", headers=_headers(), timeout=15)
    r.raise_for_status()
    return r.json()


def _catalog_fresh() -> tuple[list, list]:
    """Return (voices, models), refreshing from ElevenLabs at most every 5 min."""
    now = time.time()
    if _catalog["voices"] is None or now - _catalog["at"] > _CATALOG_TTL:
        voices = _get_json("/voices").get("voices", [])
        models_raw = _get_json("/models")
        # /models returns a bare list; be defensive if that ever changes.
        models = models_raw if isinstance(models_raw, list) else models_raw.get("models", [])
        _catalog.update(at=now, voices=voices, models=models)
    return _catalog["voices"], _catalog["models"]


@router.get("/config")
def voice_config() -> dict:
    if not _key():
        return {
            "enabled": False,
            "voices": [],
            "models": [],
            "output_formats": OUTPUT_FORMATS,
            "defaults": DEFAULTS,
        }
    try:
        voices, models = _catalog_fresh()
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else 502
        detail = "Invalid ElevenLabs API key." if code == 401 else f"ElevenLabs error ({code})."
        raise HTTPException(status_code=502, detail=detail)
    except Exception as exc:  # noqa: BLE001
        logger.warning("voice catalog fetch failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"Could not reach ElevenLabs: {exc}")

    tts_models = [m for m in models if m.get("can_do_text_to_speech")]
    defaults = dict(DEFAULTS)
    if voices:
        defaults["voice_id"] = voices[0].get("voice_id", "")

    return {
        "enabled": True,
        "voices": [
            {
                "voice_id": v.get("voice_id"),
                "name": v.get("name"),
                "category": v.get("category"),
                "labels": v.get("labels") or {},
                "preview_url": v.get("preview_url"),
            }
            for v in voices
            if v.get("voice_id")
        ],
        "models": [
            {
                "model_id": m.get("model_id"),
                "name": m.get("name") or m.get("model_id"),
                "languages": [
                    (lang.get("name") or lang.get("language_id"))
                    for lang in (m.get("languages") or [])
                ],
                "can_use_style": bool(m.get("can_use_style", True)),
                "can_use_speaker_boost": bool(m.get("can_use_speaker_boost", True)),
            }
            for m in tts_models
            if m.get("model_id")
        ],
        "output_formats": OUTPUT_FORMATS,
        "defaults": defaults,
    }


# ── TTS request model — every audio option the ElevenLabs API exposes ────────

class VoiceSettings(BaseModel):
    stability: float = Field(0.5, ge=0.0, le=1.0)
    similarity_boost: float = Field(0.75, ge=0.0, le=1.0)
    style: float = Field(0.0, ge=0.0, le=1.0)
    use_speaker_boost: bool = True
    speed: float = Field(1.0, ge=0.7, le=1.2)


class TtsRequest(BaseModel):
    text: str
    voice_id: str
    model_id: str = "eleven_flash_v2_5"
    output_format: str = "mp3_44100_128"
    stability: float = Field(0.5, ge=0.0, le=1.0)
    similarity_boost: float = Field(0.75, ge=0.0, le=1.0)
    style: float = Field(0.0, ge=0.0, le=1.0)
    use_speaker_boost: bool = True
    speed: float = Field(1.0, ge=0.7, le=1.2)
    # Optional advanced knobs. language_code only applies to Turbo/Flash v2.5.
    language_code: str | None = None
    seed: int | None = Field(None, ge=0, le=4_294_967_295)


def _stream_tts(req: TtsRequest, text: str) -> StreamingResponse:
    key = _key()
    if not key:
        raise HTTPException(
            status_code=503,
            detail="Voice is not configured — set ELEVENLABS_API_KEY in the backend.",
        )
    if not req.voice_id:
        raise HTTPException(status_code=400, detail="No voice selected.")

    body: dict = {
        "text": text,
        "model_id": req.model_id,
        "voice_settings": {
            "stability": req.stability,
            "similarity_boost": req.similarity_boost,
            "style": req.style,
            "use_speaker_boost": req.use_speaker_boost,
            "speed": req.speed,
        },
    }
    if req.language_code:
        body["language_code"] = req.language_code
    if req.seed is not None:
        body["seed"] = req.seed

    url = f"{ELEVEN_BASE}/text-to-speech/{req.voice_id}/stream?output_format={req.output_format}"
    try:
        upstream = requests.post(
            url,
            headers={**_headers(), "Content-Type": "application/json", "Accept": "audio/mpeg"},
            json=body,
            stream=True,
            timeout=(15, 300),
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach ElevenLabs: {exc}")

    if upstream.status_code != 200:
        # Error responses are small JSON — surface the message, then release.
        detail = upstream.text[:600]
        upstream.close()
        try:
            import json

            detail = json.loads(detail).get("detail", detail)
            if isinstance(detail, dict):
                detail = detail.get("message", str(detail))
        except Exception:  # noqa: BLE001
            pass
        raise HTTPException(status_code=upstream.status_code, detail=f"ElevenLabs: {detail}")

    def gen():
        try:
            for chunk in upstream.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    return StreamingResponse(gen(), media_type=_media_type(req.output_format))


@router.post("/tts")
def tts(req: TtsRequest) -> StreamingResponse:
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="No text to speak.")
    # ElevenLabs caps a single request at 40k characters (Flash/Turbo); guard so
    # we fail with a clear message instead of a raw upstream 400.
    if len(text) > 40_000:
        text = text[:40_000]
    return _stream_tts(req, text)


@router.post("/preview")
def preview(req: TtsRequest) -> StreamingResponse:
    # Ignore whatever text came in — a preview always speaks the sample sentence.
    return _stream_tts(req, PREVIEW_TEXT)
