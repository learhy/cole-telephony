"""
Voice Authentication Service — SpeechBrain ECAPA-TDNN
=====================================================
FastAPI microservice for speaker verification using ECAPA-TDNN embeddings.

Endpoints:
  GET  /health              — service health + model status
  POST /enroll              — enroll a user's voiceprint from audio
  POST /verify              — verify audio against enrolled voiceprint
  POST /enroll/session      — start an enrollment session
  POST /enroll/session/{id} — complete enrollment session
  GET  /users/{user_id}     — get user enrollment status
  DELETE /users/{user_id}   — delete user + voiceprints (privacy/GDPR)
  GET  /stats               — verification stats (FAR/FRR monitoring)
  POST /adapt/{user_id}     — template adaptation (update voiceprint on success)

Architecture:
  - SpeechBrain ECAPA-TDNN pretrained on VoxCeleb → 192-dim embeddings
  - PostgreSQL + pgvector for persistent embedding storage
  - Cosine similarity scoring with configurable threshold
  - Optional anti-spoofing pre-check via companion service
"""

import os
import io
import time
import logging
import asyncio
from typing import Optional
from contextlib import asynccontextmanager

import torch
import torchaudio
import numpy as np
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Header, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import psycopg2
from pgvector.psycopg2 import register_vector
import httpx

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DATABASE_URL = os.environ["DATABASE_URL"]
DEVICE = os.environ.get("DEVICE", "cpu")
THRESHOLD = float(os.environ.get("THRESHOLD", "0.25"))
MIN_AUDIO_SEC = float(os.environ.get("MIN_AUDIO_DURATION_SEC", "5"))
MAX_AUDIO_SEC = float(os.environ.get("MAX_AUDIO_DURATION_SEC", "120"))
AUTH_API_KEY = os.environ["AUTH_API_KEY"]
ANTI_SPOOF_URL = os.environ.get("ANTI_SPOOF_URL", "http://anti-spoof:8002")
SAMPLE_RATE = 16000

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("voice-auth")

# ---------------------------------------------------------------------------
# Model loading (lazy, on startup)
# ---------------------------------------------------------------------------

verifier = None
_model_lock = asyncio.Lock()

def get_verifier():
    """Lazily load the SpeechBrain ECAPA-TDNN model."""
    global verifier
    if verifier is None:
        from speechbrain.inference.speaker import SpeakerRecognition
        log.info("Loading SpeechBrain ECAPA-TDNN model (first call, downloads ~80MB)...")
        verifier = SpeakerRecognition.from_hparams(
            source="speechbrain/spkrec-ecapa-voxceleb",
            savedir="/app/models/spkrec-ecapa",
            run_opts={"device": DEVICE},
        )
        log.info("Model loaded successfully on %s", DEVICE)
    return verifier


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db():
    """Get a DB connection. FastAPI dependency."""
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    try:
        register_vector(conn)
    except psycopg2.ProgrammingError:
        # vector type not yet installed — create it and retry
        conn.rollback()
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        conn.autocommit = False
        register_vector(conn)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Ensure tables exist. Runs on startup — handles both fresh and existing DBs."""
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    register_vector(conn)
    # Create extension first (pgvector image has it, but be explicit)
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    # Then run schema
    try:
        with open("/app/init.sql") as f:
            schema = f.read()
        # Skip the CREATE EXTENSION line since we already did it
        conn.execute(schema)
    except Exception as e:
        log.warning("Schema creation from file failed (tables may already exist): %s", e)
        # Fall back to executing statements one by one
        try:
            with open("/app/init.sql") as f:
                schema = f.read()
            # Remove the CREATE EXTENSION line (already done)
            lines = [l for l in schema.split("\n") if not l.strip().startswith("CREATE EXTENSION")]
            cleaned = "\n".join(lines)
            conn.execute(cleaned)
        except Exception as e2:
            log.warning("Schema creation fallback also failed: %s", e2)
    conn.close()
    log.info("Database initialized")


# ---------------------------------------------------------------------------
# Audio processing
# ---------------------------------------------------------------------------

def load_audio(file_bytes: bytes) -> torch.Tensor:
    """Load audio bytes → 16kHz mono tensor. Raises HTTPException on invalid audio."""
    try:
        wav, sr = torchaudio.load(io.BytesIO(file_bytes))
    except Exception as e1:
        # Try soundfile fallback (handles more formats)
        try:
            import soundfile as sf
            data, sr = sf.read(io.BytesIO(file_bytes))
            if data.ndim > 1:
                data = data.mean(axis=1)  # stereo → mono
            wav = torch.from_numpy(data).float().unsqueeze(0)
            sr = int(sr)
        except Exception as e2:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid or corrupted audio file. torchaudio error: {e1}; soundfile error: {e2}",
            )

    # Stereo → mono
    if wav.shape[0] > 1:
        wav = wav.mean(dim=0, keepdim=True)

    # Resample to 16kHz
    if sr != SAMPLE_RATE:
        wav = torchaudio.functional.resample(wav, sr, SAMPLE_RATE)

    duration = wav.shape[1] / SAMPLE_RATE
    if duration < MIN_AUDIO_SEC:
        raise HTTPException(
            status_code=400,
            detail=f"Audio too short: {duration:.1f}s, need at least {MIN_AUDIO_SEC}s",
        )
    if duration > MAX_AUDIO_SEC:
        # Truncate to max
        wav = wav[:, : int(MAX_AUDIO_SEC * SAMPLE_RATE)]

    return wav


def extract_embedding(wav: torch.Tensor) -> np.ndarray:
    """Extract 192-dim ECAPA-TDNN embedding from audio tensor.
    
    SpeechBrain's encode_batch expects (batch, time_dim) shape.
    Input wav is (1, samples) mono at 16kHz.
    """
    model = get_verifier()
    with torch.no_grad():
        # encode_batch expects (batch, time) — squeeze channel dim
        if wav.dim() == 2 and wav.shape[0] == 1:
            wav_in = wav.squeeze(0)  # (time,)
        elif wav.dim() == 1:
            wav_in = wav
        else:
            wav_in = wav.squeeze(0)  # fallback
        
        # encode_batch handles adding batch dim internally
        embedding = model.encode_batch(wav_in)
        # Output shape: (1, 1, 192) → squeeze to (192,)
        return embedding.squeeze().cpu().numpy()


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity between two embeddings."""
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-8))


# ---------------------------------------------------------------------------
# Anti-spoofing check (calls companion service)
# ---------------------------------------------------------------------------

async def check_anti_spoof(audio_bytes: bytes) -> dict:
    """Call AASIST anti-spoofing service. Returns {score, decision} or {decision: 'unknown'}."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{ANTI_SPOOF_URL}/detect",
                headers={"X-API-Key": AUTH_API_KEY},
                files={"audio": ("audio.wav", audio_bytes, "audio/wav")},
            )
            if resp.status_code == 200:
                return resp.json()
            log.warning("Anti-spoof service returned %d", resp.status_code)
    except Exception as e:
        log.warning("Anti-spoof check failed: %s", e)
    return {"score": None, "decision": "unknown"}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

async def verify_api_key(x_api_key: Optional[str] = Header(None)):
    """Simple API key auth for service-to-service calls."""
    if x_api_key != AUTH_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    return True


# ---------------------------------------------------------------------------
# API Models
# ---------------------------------------------------------------------------

class EnrollResponse(BaseModel):
    status: str
    user_id: str
    embedding_dim: int
    audio_duration: float


class VerifyResponse(BaseModel):
    user_id: str
    score: float
    threshold: float
    decision: str  # accept | reject
    spoof_score: Optional[float] = None
    spoof_decision: Optional[str] = None
    final_decision: str  # authenticated | rejected
    audio_duration: Optional[float] = None


class SessionStart(BaseModel):
    user_id: str
    display_name: Optional[str] = None
    phone_number: Optional[str] = None
    call_id: Optional[str] = None


class SessionComplete(BaseModel):
    session_id: int
    status: str = "completed"


class AdaptRequest(BaseModel):
    audio: bytes  # handled as UploadFile in endpoint


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Voice Auth Service starting up...")
    # Pre-load model in background so first request isn't blocked too long
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, get_verifier)
    # Init DB
    try:
        init_db()
    except Exception as e:
        log.warning("DB init from file failed (tables may already exist): %s", e)
    yield
    log.info("Voice Auth Service shutting down.")


app = FastAPI(
    title="Voice Authentication Service",
    description="SpeechBrain ECAPA-TDNN speaker verification with pgvector storage",
    version="2.0.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health():
    """Health check — reports model and DB status."""
    model_loaded = verifier is not None
    db_ok = False
    try:
        conn = psycopg2.connect(DATABASE_URL)
        conn.close()
        db_ok = True
    except Exception:
        pass
    return {
        "status": "ok",
        "device": DEVICE,
        "model_loaded": model_loaded,
        "db_connected": db_ok,
        "threshold": THRESHOLD,
    }


@app.post("/enroll", response_model=EnrollResponse, dependencies=[Depends(verify_api_key)])
async def enroll(
    user_id: str = Form(...),
    audio: UploadFile = File(...),
    display_name: str = Form(None),
    phone_number: str = Form(None),
    session_type: str = Form("enrollment"),
    db=Depends(get_db),
):
    """
    Enroll a voiceprint. Accepts audio file (wav/mp3/flac), extracts ECAPA-TDNN
    embedding, stores in PostgreSQL with pgvector.

    If user already enrolled, this adds an additional sample (for multi-sample
    enrollment or template adaptation).
    """
    audio_bytes = await audio.read()
    wav = load_audio(audio_bytes)
    duration = wav.shape[1] / SAMPLE_RATE
    embedding = extract_embedding(wav)

    # Upsert user
    with db.cursor() as cur:
        cur.execute(
            """INSERT INTO voice_users (user_id, display_name, phone_number, enrolled, enrolled_at, enrollment_count)
               VALUES (%s, %s, %s, TRUE, NOW(), 1)
               ON CONFLICT (user_id) DO UPDATE
               SET enrolled = TRUE,
                   enrolled_at = COALESCE(voice_users.enrolled_at, NOW()),
                   enrollment_count = voice_users.enrollment_count + 1,
                   display_name = COALESCE(EXCLUDED.display_name, voice_users.display_name),
                   phone_number = COALESCE(EXCLUDED.phone_number, voice_users.phone_number),
                   updated_at = NOW()
               RETURNING enrolled_at
            """,
            (user_id, display_name, phone_number),
        )
        cur.fetchone()

        # Store embedding
        cur.execute(
            """INSERT INTO voiceprints (user_id, embedding, audio_duration, session_type)
               VALUES (%s, %s, %s, %s)
            """,
            (user_id, embedding, duration, session_type),
        )

    log.info("Enrolled user=%s duration=%.1fs dim=%d", user_id, duration, embedding.shape[0])
    return EnrollResponse(
        status="enrolled",
        user_id=user_id,
        embedding_dim=embedding.shape[0],
        audio_duration=duration,
    )


@app.post("/verify", response_model=VerifyResponse, dependencies=[Depends(verify_api_key)])
async def verify(
    user_id: str = Form(...),
    audio: UploadFile = File(...),
    run_anti_spoof: bool = Form(True),
    passphrase_ok: bool = Form(None),
    call_id: str = Form(None),
    db=Depends(get_db),
):
    """
    Verify audio against enrolled voiceprint.

    Pipeline:
    1. (Optional) Anti-spoofing check via AASIST service
    2. Extract ECAPA-TDNN embedding from test audio
    3. Compare against enrolled embedding (cosine similarity)
    4. Decision: accept/reject based on threshold
    5. Final decision combines voiceprint + anti-spoof + passphrase

    Returns score, decision, and audit info.
    """
    audio_bytes = await audio.read()
    wav = load_audio(audio_bytes)
    duration = wav.shape[1] / SAMPLE_RATE

    # Check user is enrolled
    with db.cursor() as cur:
        cur.execute(
            "SELECT enrolled FROM voice_users WHERE user_id = %s",
            (user_id,),
        )
        row = cur.fetchone()
        if not row or not row[0]:
            raise HTTPException(status_code=404, detail="User not enrolled")

        # Get latest enrolled embedding (or average of all enrollment embeddings)
        cur.execute(
            """SELECT embedding FROM voiceprints
               WHERE user_id = %s AND session_type = 'enrollment'
               ORDER BY created_at DESC LIMIT 1
            """,
            (user_id,),
        )
        enrolled_row = cur.fetchone()
        if not enrolled_row:
            raise HTTPException(status_code=404, detail="No voiceprint found for user")

        enrolled_embedding = enrolled_row[0].to_numpy() if hasattr(enrolled_row[0], "to_numpy") else np.array(enrolled_row[0], dtype=np.float32)

    # Extract test embedding
    test_embedding = extract_embedding(wav)

    # Cosine similarity
    score = cosine_similarity(enrolled_embedding, test_embedding)
    decision = "accept" if score >= THRESHOLD else "reject"

    # Anti-spoofing check
    spoof_result = {"score": None, "decision": "unknown"}
    if run_anti_spoof:
        spoof_result = await check_anti_spoof(audio_bytes)

    # Final decision: voiceprint accept AND passphrase ok
    # Anti-spoof is ADVISORY on phone calls — AASIST false-positives on codec-compressed audio
    voice_ok = decision == "accept"
    spoof_ok = spoof_result["decision"] in ("bonafide", "unknown")  # unknown = don't block if service down
    pass_ok = passphrase_ok if passphrase_ok is not None else True  # if not provided, don't gate

    # If voiceprint matched and passphrase passed, authenticate even if anti-spoof flagged
    # (phone codec compression causes AASIST false positives — see logs from 2026-07-25)
    if voice_ok and pass_ok:
        final = "authenticated"
    elif voice_ok and not spoof_ok:
        # Voice matched but anti-spoof flagged and no passphrase — log but accept with warning
        log.warning("Voiceprint matched but anti-spoof flagged spoof (score=%.4f) — accepting with passphrase gate",
                     spoof_result.get("score", 0))
        final = "authenticated" if pass_ok else "rejected"
    else:
        final = "rejected"

    # Log verification
    with db.cursor() as cur:
        cur.execute(
            """INSERT INTO verification_log
               (user_id, score, threshold, decision, spoof_score, spoof_decision,
                audio_duration, passphrase_ok, final_decision, call_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (user_id, score, THRESHOLD, decision,
             spoof_result.get("score"), spoof_result.get("decision"),
             duration, passphrase_ok, final, call_id),
        )
        # Update user stats
        cur.execute(
            """UPDATE voice_users
               SET verify_count = verify_count + 1,
                   last_verified_at = CASE WHEN %s = 'authenticated' THEN NOW() ELSE last_verified_at END,
                   reject_count = reject_count + CASE WHEN %s = 'rejected' THEN 1 ELSE 0 END,
                   updated_at = NOW()
               WHERE user_id = %s
            """,
            (final, final, user_id),
        )

    log.info("Verify user=%s score=%.4f decision=%s spoof=%s final=%s",
             user_id, score, decision, spoof_result.get("decision"), final)

    return VerifyResponse(
        user_id=user_id,
        score=round(score, 4),
        threshold=THRESHOLD,
        decision=decision,
        spoof_score=spoof_result.get("score"),
        spoof_decision=spoof_result.get("decision"),
        final_decision=final,
        audio_duration=duration,
    )


@app.post("/adapt/{user_id}", dependencies=[Depends(verify_api_key)])
async def adapt_template(
    user_id: str,
    audio: UploadFile = File(...),
    db=Depends(get_db),
):
    """
    Template adaptation: after a successful verification, update the voiceprint
    with the new sample. This handles voice drift over time (colds, aging).

    Stores as 'adaptation' session type. The verification endpoint uses the
    most recent 'enrollment' embedding; for rolling-average adaptation, see
    /adapt which can update the primary template.
    """
    audio_bytes = await audio.read()
    wav = load_audio(audio_bytes)
    duration = wav.shape[1] / SAMPLE_RATE
    embedding = extract_embedding(wav)

    with db.cursor() as cur:
        # Get current primary embedding
        cur.execute(
            """SELECT embedding FROM voiceprints
               WHERE user_id = %s AND session_type = 'enrollment'
               ORDER BY created_at DESC LIMIT 1
            """,
            (user_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="User not enrolled")

        current = np.array(row[0])
        # 80/20 blend: keep 80% old, 20% new — slow drift
        adapted = 0.8 * current + 0.2 * embedding
        adapted = adapted / (np.linalg.norm(adapted) + 1e-8)

        # Update the latest enrollment embedding in-place
        cur.execute(
            """UPDATE voiceprints SET embedding = %s, audio_duration = %s
               WHERE id = (
                   SELECT id FROM voiceprints
                   WHERE user_id = %s AND session_type = 'enrollment'
                   ORDER BY created_at DESC LIMIT 1
               )
            """,
            (adapted, duration, user_id),
        )

    return {"status": "adapted", "user_id": user_id, "blend": "80/20"}


@app.post("/enroll/session", dependencies=[Depends(verify_api_key)])
async def start_enrollment_session(
    session: SessionStart,
    db=Depends(get_db),
):
    """Start an enrollment session — creates user record (if new) and session tracker."""
    with db.cursor() as cur:
        cur.execute(
            """INSERT INTO voice_users (user_id, display_name, phone_number)
               VALUES (%s, %s, %s)
               ON CONFLICT (user_id) DO UPDATE
               SET display_name = COALESCE(EXCLUDED.display_name, voice_users.display_name),
                   phone_number = COALESCE(EXCLUDED.phone_number, voice_users.phone_number),
                   updated_at = NOW()
            """,
            (session.user_id, session.display_name, session.phone_number),
        )
        cur.execute(
            """INSERT INTO enrollment_sessions (user_id, call_id)
               VALUES (%s, %s) RETURNING id
            """,
            (session.user_id, session.call_id),
        )
        session_id = cur.fetchone()[0]

    return {"session_id": session_id, "user_id": session.user_id, "status": "pending"}


@app.post("/enroll/session/{session_id}", dependencies=[Depends(verify_api_key)])
async def complete_enrollment_session(
    session_id: int,
    body: SessionComplete,
    db=Depends(get_db),
):
    """Mark enrollment session as completed or failed."""
    with db.cursor() as cur:
        cur.execute(
            """UPDATE enrollment_sessions
               SET status = %s, completed_at = NOW()
               WHERE id = %s
            """,
            (body.status, session_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Session not found")

    return {"session_id": session_id, "status": body.status}


@app.get("/users/{user_id}", dependencies=[Depends(verify_api_key)])
async def get_user(user_id: str, db=Depends(get_db)):
    """Get user enrollment status and stats."""
    with db.cursor() as cur:
        cur.execute(
            """SELECT user_id, display_name, phone_number, enrolled, enrolled_at,
                      enrollment_count, verify_count, reject_count, last_verified_at
               FROM voice_users WHERE user_id = %s
            """,
            (user_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="User not found")

        return {
            "user_id": row[0],
            "display_name": row[1],
            "phone_number": row[2],
            "enrolled": row[3],
            "enrolled_at": row[4],
            "enrollment_count": row[5],
            "verify_count": row[6],
            "reject_count": row[7],
            "last_verified_at": row[8],
        }


@app.delete("/users/{user_id}", dependencies=[Depends(verify_api_key)])
async def delete_user(user_id: str, db=Depends(get_db)):
    """Delete user and all voiceprints — GDPR/BIPA right to deletion."""
    with db.cursor() as cur:
        cur.execute("DELETE FROM voice_users WHERE user_id = %s", (user_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="User not found")

    log.info("Deleted user %s and all associated voiceprints", user_id)
    return {"status": "deleted", "user_id": user_id}


@app.get("/stats", dependencies=[Depends(verify_api_key)])
async def stats(db=Depends(get_db)):
    """Verification statistics for FAR/FRR monitoring and threshold tuning."""
    with db.cursor() as cur:
        cur.execute(
            """SELECT
                 COUNT(*) as total,
                 COUNT(*) FILTER (WHERE final_decision = 'authenticated') as accepts,
                 COUNT(*) FILTER (WHERE final_decision = 'rejected') as rejects,
                 AVG(score) as avg_score,
                 MIN(score) as min_score,
                 MAX(score) as max_score
               FROM verification_log
               WHERE created_at > NOW() - INTERVAL '30 days'
            """,
        )
        row = cur.fetchone()
        return {
            "total_verifications": row[0],
            "authenticated": row[1],
            "rejected": row[2],
            "avg_score": float(row[3]) if row[3] else None,
            "min_score": float(row[4]) if row[4] else None,
            "max_score": float(row[5]) if row[5] else None,
            "threshold": THRESHOLD,
        }


