"""
AASIST Anti-Spoofing Service
=============================
Voice clone / replay / synthetic speech detection using the AASIST model
(Anti-spoofing by Aggregating Spectral and Temporal representations).

This service wraps the AASIST model in a FastAPI endpoint. When the full
AASIST model weights aren't available, it falls back to a signal-based
heuristic detector so the pipeline still functions end-to-end.

Endpoint:
  GET  /health    — service health
  POST /detect    — detect if audio is bonafide or spoofed

Returns:
  { "score": float, "decision": "bonafide" | "spoof" }
  score: 0.0 (likely spoof) to 1.0 (likely bonafide)
"""

import os
import io
import logging
import asyncio
from typing import Optional

import torch
import torch.nn.functional as F
import torchaudio
import numpy as np
from fastapi import FastAPI, UploadFile, File, Header, HTTPException, Depends
from contextlib import asynccontextmanager

DEVICE = os.environ.get("DEVICE", "cpu")
AUTH_API_KEY = os.environ["AUTH_API_KEY"]
SAMPLE_RATE = 16000

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("anti-spoof")

# ---------------------------------------------------------------------------
# AASIST model (lazy load)
# ---------------------------------------------------------------------------

aasist_model = None
_aasist_available = False

# AASIST model architecture config from the official config/AASIST.conf
AASIST_CONFIG = {
    "nb_samp": 64600,
    "first_conv": 128,
    "filts": [70, [1, 32], [32, 32], [32, 64], [64, 64]],
    "gat_dims": [64, 32],
    "pool_ratios": [0.5, 0.7, 0.5, 0.5],
    "temperatures": [2.0, 2.0, 100.0, 100.0],
}

def load_aasist():
    """Load the AASIST model with pretrained weights from the cloned repo."""
    global aasist_model, _aasist_available
    if aasist_model is not None or _aasist_available:
        return
    try:
        import sys
        sys.path.insert(0, "/app/aasist")
        from models.AASIST import Model

        weights_path = "/app/aasist/models/weights/AASIST.pth"
        if not os.path.exists(weights_path):
            log.warning("AASIST weights not found at %s, using signal-based fallback", weights_path)
            _aasist_available = False
            return

        aasist_model = Model(AASIST_CONFIG)
        state_dict = torch.load(weights_path, map_location=DEVICE)
        # Weights file is a raw OrderedDict state_dict
        if isinstance(state_dict, dict) and "model" in state_dict:
            aasist_model.load_state_dict(state_dict["model"])
        else:
            aasist_model.load_state_dict(state_dict)
        aasist_model.to(DEVICE).eval()
        _aasist_available = True
        log.info("AASIST model loaded successfully with pretrained weights")
    except Exception as e:
        log.warning("AASIST model load failed (using fallback): %s", e)
        _aasist_available = False


# ---------------------------------------------------------------------------
# Signal-based fallback detector
# ---------------------------------------------------------------------------

def signal_based_detect(wav: torch.Tensor) -> dict:
    """
    Heuristic anti-spoofing when AASIST model isn't available.
    Checks for synthetic speech artifacts:
    - Spectral flatness (synthetic audio often too smooth/flat)
    - Zero-crossing rate variance (natural speech varies more)
    - Energy distribution (clones often lack natural dynamics)
    """
    import librosa

    audio = wav.squeeze().numpy()
    sr = SAMPLE_RATE

    # Spectral flatness — synthetic audio tends to be less flat (more tonal)
    flatness = librosa.feature.spectral_flatness(y=audio, hop_length=512)
    mean_flatness = float(np.mean(flatness))

    # Zero-crossing rate — natural speech has higher ZCR variance
    zcr = librosa.feature.zero_crossing_rate(audio, hop_length=512)
    zcr_var = float(np.var(zcr))

    # RMS energy variance — natural speech has more dynamic range
    rms = librosa.feature.rms(y=audio, hop_length=512)
    rms_var = float(np.var(rms))

    # Spectral centroid — synthetic audio may have different frequency distribution
    centroid = librosa.feature.spectral_centroid(y=audio, sr=sr, hop_length=512)
    centroid_var = float(np.var(centroid))

    # Heuristic scoring:
    # Higher flatness variance, ZCR variance, RMS variance → more likely bonafide
    # These thresholds are rough — AASIST should be used for production
    score = 0.5
    score += min(0.15, mean_flatness * 0.3)
    score += min(0.15, zcr_var * 100)
    score += min(0.1, rms_var * 50)
    score += min(0.1, centroid_var / 1e6)
    score = max(0.0, min(1.0, score))

    decision = "bonafide" if score >= 0.5 else "spoof"

    return {
        "score": round(score, 4),
        "decision": decision,
        "method": "signal_heuristic",
        "features": {
            "spectral_flatness": round(mean_flatness, 4),
            "zcr_variance": round(zcr_var, 6),
            "rms_variance": round(rms_var, 6),
            "centroid_variance": round(centroid_var, 2),
        }
    }


def aasist_detect(wav: torch.Tensor) -> dict:
    """Run AASIST model on audio.
    Model.forward expects (batch, time) and internally unsqueezes channel dim.
    Returns (last_hidden, output) where output is logits [bonafide, spoof].
    """
    with torch.no_grad():
        x = wav.to(DEVICE)
        # Model.forward does x.unsqueeze(1) internally, so we need (batch, time)
        if x.dim() == 3:
            x = x.squeeze(1)  # (1, 1, time) -> (1, time)
        elif x.dim() == 2 and x.shape[0] > 1:
            x = x.mean(dim=0, keepdim=True)  # stereo -> mono, keep batch
        elif x.dim() == 1:
            x = x.unsqueeze(0)  # (time,) -> (1, time)
        # Pad/truncate to nb_samp (64600 samples ~4s @16kHz)
        nb_samp = AASIST_CONFIG["nb_samp"]
        if x.shape[-1] < nb_samp:
            x = F.pad(x, (0, nb_samp - x.shape[-1]))
        else:
            x = x[..., :nb_samp]
        _, output = aasist_model(x)
        probs = torch.softmax(output, dim=-1)
        bonafide_prob = float(probs[0][0])

    return {
        "score": round(bonafide_prob, 4),
        "decision": "bonafide" if bonafide_prob >= 0.5 else "spoof",
        "method": "aasist"
    }


# ---------------------------------------------------------------------------
# Audio loading
# ---------------------------------------------------------------------------

def load_audio(file_bytes: bytes) -> torch.Tensor:
    try:
        wav, sr = torchaudio.load(io.BytesIO(file_bytes))
    except Exception as e1:
        try:
            import soundfile as sf
            data, sr = sf.read(io.BytesIO(file_bytes))
            if data.ndim > 1:
                data = data.mean(axis=1)
            wav = torch.from_numpy(data).float().unsqueeze(0)
            sr = int(sr)
        except Exception as e2:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid or corrupted audio file. torchaudio error: {e1}; soundfile error: {e2}",
            )

    if wav.shape[0] > 1:
        wav = wav.mean(dim=0, keepdim=True)
    if sr != SAMPLE_RATE:
        wav = torchaudio.functional.resample(wav, sr, SAMPLE_RATE)
    # Max 60s for anti-spoofing
    if wav.shape[1] > 60 * SAMPLE_RATE:
        wav = wav[:, : 60 * SAMPLE_RATE]
    return wav


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

async def verify_api_key(x_api_key: Optional[str] = Header(None)):
    if x_api_key != AUTH_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


# ---------------------------------------------------------------------------
# FastAPI
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Anti-Spoof Service starting up...")
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, load_aasist)
    yield
    log.info("Anti-Spoof Service shutting down.")


app = FastAPI(
    title="AASIST Anti-Spoofing Service",
    description="Voice clone / synthetic / replay detection",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "device": DEVICE,
        "aasist_loaded": _aasist_available,
        "fallback_active": not _aasist_available,
    }


@app.post("/detect", dependencies=[Depends(verify_api_key)])
async def detect(audio: UploadFile = File(...)):
    """
    Detect if audio is bonafide (real human) or spoofed (synthetic/replay/clone).

    Returns score (0=spoof, 1=bonafide) and decision.
    Uses AASIST model if available, otherwise signal-based heuristic.
    """
    audio_bytes = await audio.read()
    wav = load_audio(audio_bytes)

    if _aasist_available:
        result = aasist_detect(wav)
    else:
        result = signal_based_detect(wav)

    log.info("Anti-spoof: decision=%s score=%.4f method=%s",
             result["decision"], result["score"], result.get("method", "aasist"))
    return result