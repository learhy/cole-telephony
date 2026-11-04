# Impostor Score Calibration — 2026-10-23

## Experiment 1: File-based bench test (clean WAVs → /verify endpoint)

| Sample | Score | Spoof | Decision |
|---|---|---|---|
| attack-replay (espeak, passphrase style) | 0.0780 | bonafide | reject |
| attack-replay (Samantha-analog) | -0.0193 | spoof | reject |
| TTS female (aria/samantha-analog) | -0.0380 | spoof | reject |
| TTS female (jenny) | -0.0924 | spoof | reject |
| TTS male (christopher) | 0.0331 | spoof | reject |
| TTS male (guy) | 0.0355 | spoof | reject |
| TTS robotic (espeak default) | 0.0776 | bonafide | reject |
| TTS robotic (espeak f5) | 0.0919 | bonafide | reject |
| **POSITIVE CONTROL: dan_enrollment.wav** | **-0.0421** | spoof | reject |

Impostor range: -0.09 … +0.09 (mean 0.021).

## Key findings

1. **File-based scores are NOT representative of the in-call path.** Bench TTS
   scores ~0.00±0.09 vs in-call TTS 0.30–0.37. The 8kHz PSTN codec path is
   what inflates similarity; clean-audio bench tests understate impostor risk.
2. **The positive control FAILED:** Dan's enrollment-era WAV scored -0.0421
   against his own stored template — statistically indistinguishable from
   impostors. Either the wav is not the audio the 2026-07-25 template was
   built from, or the template/enroll pipeline has a provenance problem.
   Do NOT treat this wav as ground truth.
3. **The only trustworthy numbers are IN-CALL scores from verification_log:**
   - Dan real (Aug 2–3, old call path): 0.531–0.745 — 1 authenticated/bonafide
   - Dan real (today, Twilio PSTN path): 0.078–0.410
   - say-TTS impostor (today, in-call): 0.301–0.367
   → On today's path, genuine and impostor distributions OVERLAP. No
   threshold separates them. AASIST flagged 18/18 in-call events as spoof
   (including all real calls) — non-discriminative on this path.

## Recommendations

- MATCH_THRESHOLD cannot be set meaningfully today. The biometric factor is
  ADVISORY ONLY until re-enrollment.
- Re-enroll on the CURRENT call path: ≥60s across ≥2 separate calls
  (multi-session flow in agent_voice_auth.py). Old stale template is cleared
  at first new capture.
- After re-enrollment, re-measure in-call: real voice vs the stored TTS
  attack samples replayed through a real call. Only if a wide margin exists
  (real min ≥ impostor max + ~0.15) should the biometric regain gate status.
- The dependable factors are: passphrase (something you know) + Telegram
  out-of-band approval (something you have) for sensitive actions.
