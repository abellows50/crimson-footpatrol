"""
Audio enhancement before Whisper.

Scanner audio (trunked radio, re-encoded to low-bitrate MP3) has a few typical problems: hiss and
background noise (sirens, engines, wind), dispatch alert tones, and words that are much quieter than
others in the same transmission. Each step below targets one of those, gently, because heavy
processing (aggressive noise removal) makes Whisper worse, not better.

  1. band-pass       keep the radio voice band (~150-4000 Hz): removes hum, rumble and hiss
  2. tone removal    notch out a steady tone that runs through the whole clip, gaps included
                     (alert tones, whine); voices never do that, so speech is left alone
  3. noise reduction spectral gating against the clip's own noise floor, capped at about -12 dB,
                     and only on clips that are actually noisy (clean ones are left untouched)
  4. normalize, pad  steady overall level; silence at both ends so first/last words aren't clipped

Tested on synthetic radio clips with Whisper: tone notching that also fired on voiced speech, and
automatic leveling, both made recognition worse, so tone removal is strict and leveling is off.

enhance(audio)        -> the full chain
condition(audio)      -> only steps 1 and 5 (the older, plain conditioning; used as a fallback)
"""
import numpy as np

SR = 16000
N_FFT, HOP = 512, 128


# ---------------------------------------------------------------------------- STFT helpers
def _stft(x):
    win = np.hanning(N_FFT).astype(np.float32)
    pad = N_FFT // 2
    xp = np.pad(x, (pad, pad + N_FFT), mode="constant")
    n = 1 + (len(xp) - N_FFT) // HOP
    idx = np.arange(N_FFT)[None, :] + HOP * np.arange(n)[:, None]
    return np.fft.rfft(xp[idx] * win, axis=1), win


def _istft(S, win, length):
    frames = np.fft.irfft(S, N_FFT, axis=1) * win
    pad = N_FFT // 2
    out = np.zeros(HOP * (len(frames) - 1) + N_FFT, dtype=np.float32)
    norm = np.zeros_like(out)
    for i, fr in enumerate(frames):
        out[i * HOP:i * HOP + N_FFT] += fr
        norm[i * HOP:i * HOP + N_FFT] += win ** 2
    out /= np.maximum(norm, 1e-6)
    return out[pad:pad + length]


def _smooth(a, k, axis):
    if k <= 1:
        return a
    ker = np.ones(k, dtype=np.float32) / k
    return np.apply_along_axis(lambda v: np.convolve(v, ker, mode="same"), axis, a)


# ---------------------------------------------------------------------------- steps
def bandpass(x, lo=150.0, hi=4000.0):
    X = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1 / SR)
    band = np.clip((f - (lo - 30)) / 80, 0, 1) * np.clip((hi + 300 - f) / 500, 0, 1)
    return np.fft.irfft(X * band, len(x)).astype(np.float32)


def snr_db(x, frame=400):
    """Rough signal-to-noise of a clip: loud frames (speech) vs quiet frames (the noise floor)."""
    n = len(x) // frame
    if n < 6:
        return 99.0
    rms = np.sqrt(np.mean(x[:n * frame].reshape(n, frame) ** 2, axis=1) + 1e-12)
    return float(20 * np.log10(np.percentile(rms, 90) / max(np.percentile(rms, 15), 1e-9)))


def spectral_clean(x, tones=True, denoise=True, floor_db=-12.0, over=1.5, stats=None):
    """Tone removal + spectral-gating noise reduction in one STFT pass."""
    stats = {} if stats is None else stats
    stats.setdefault("tones", 0)
    stats.setdefault("denoised", False)
    if len(x) < N_FFT * 2:
        return x
    S, win = _stft(x)
    P = np.abs(S) ** 2 + 1e-12                                   # frames x bins
    nb = P.shape[1]
    gain = np.ones_like(P, dtype=np.float32)
    f = np.fft.rfftfreq(N_FFT, 1 / SR)
    voice = (f > 120) & (f < 4300)

    if tones and P.shape[0] >= 8:
        # a steady tone = a bin that stands well above its neighbours in most frames
        logp = 10 * np.log10(P)
        neigh = _smooth(logp, 13, axis=1)
        peaky = (logp - neigh) > 12                              # 12 dB above the local spectrum
        occupancy = peaky.mean(axis=0)
        tone_bins = np.where((occupancy > 0.85) & voice)[0]      # present nearly all the time, gaps too
        tone_bins = tone_bins[np.argsort(-occupancy[tone_bins])][:4]
        stats["tones"] = int(len(tone_bins))
        for b in tone_bins:
            lo, hi = max(0, b - 1), min(nb, b + 2)
            rows = peaky[:, b]
            gain[rows, lo:hi] = np.minimum(gain[rows, lo:hi], 10 ** (-25 / 20))

    if denoise:
        # noise floor per frequency = a low percentile of that bin over the clip
        noise = np.percentile(P, 12, axis=0)
        noise = _smooth(noise[None, :], 5, axis=1)[0]
        snr = P / (over * noise[None, :])
        g = np.clip(1 - 1 / np.maximum(snr, 1e-6), 10 ** (floor_db / 20), 1.0)   # Wiener-style gain
        g = _smooth(_smooth(g, 3, axis=1), 3, axis=0)                           # avoid "musical noise"
        gain = np.minimum(gain, g.astype(np.float32))
        stats["denoised"] = True

    if not stats["tones"] and not stats["denoised"]:
        return x                                                 # nothing to do: skip the round trip
    return _istft(S * gain, win, len(x)).astype(np.float32)


def level(x, target=0.1, max_boost=4.0, frame=320):
    """Slow automatic gain on the speech envelope (20 ms frames, ~150 ms smoothing).
    Only frames that are clearly above the clip's quiet floor are lifted, so static isn't pumped up."""
    n = len(x) // frame
    if n < 4:
        return x
    rms = np.sqrt(np.mean(x[:n * frame].reshape(n, frame) ** 2, axis=1) + 1e-12)
    floor = np.percentile(rms, 20)
    speech = np.percentile(rms, 90)
    env = _smooth(rms[None, :], 7, axis=1)[0]
    g = np.clip(speech / np.maximum(env, 1e-9), 1.0, max_boost)
    g[env < floor * 2.5] = 1.0                                   # leave the gaps alone
    g = _smooth(g[None, :], 5, axis=1)[0]
    gs = np.interp(np.arange(len(x)), np.arange(n) * frame + frame / 2, g).astype(np.float32)
    return x * gs


def normalize_pad(x, pad_s=0.4):
    frame = 400
    n = len(x) // frame
    if n:
        rms = np.sqrt(np.mean(x[:n * frame].reshape(n, frame) ** 2, axis=1) + 1e-12)
        speech_rms = float(np.percentile(rms, 80))
    else:
        speech_rms = float(np.sqrt(np.mean(x ** 2)) + 1e-12)
    if speech_rms > 1e-5:
        x = x * min(0.1 / speech_rms, 60.0)
    peak = float(np.abs(x).max()) if len(x) else 0
    if peak > 0.97:
        x = x * (0.97 / peak)
    pad = np.zeros(int(pad_s * SR), dtype=np.float32)
    return np.concatenate([pad, x.astype(np.float32), pad])


# ---------------------------------------------------------------------------- chains
def condition(audio):
    if audio.size == 0:
        return audio
    x = audio.astype(np.float32) - float(np.mean(audio))
    return normalize_pad(bandpass(x))


def enhance(audio, noisy_below_db=24.0, stats=None):
    """stats (optional dict) is filled with what was done: snr_db, tones, denoised."""
    stats = {} if stats is None else stats
    if audio.size == 0:
        return audio
    x = audio.astype(np.float32) - float(np.mean(audio))
    x = bandpass(x)
    stats["snr_db"] = round(snr_db(x), 1)
    x = spectral_clean(x, tones=True, denoise=stats["snr_db"] < noisy_below_db, stats=stats)
    return normalize_pad(x)
