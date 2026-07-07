# -*- coding: utf-8 -*-
import imageio_ffmpeg, subprocess, os, wave, glob
import numpy as np

REC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "recordings")
exe = imageio_ffmpeg.get_ffmpeg_exe()

m4as = glob.glob(os.path.join(REC, "*.m4a"))
print("M4A files:", [os.path.basename(x) for x in m4as])
if not m4as:
    raise SystemExit("no m4a found")
src = max(m4as, key=os.path.getmtime)
print("Using:", os.path.basename(src))

dst = os.path.join(REC, "duimian_orig.wav")
dst8 = os.path.join(REC, "duimian_8k.wav")
r1 = subprocess.run([exe, "-y", "-i", src, "-ac", "1", dst], capture_output=True, text=True)
r2 = subprocess.run([exe, "-y", "-i", src, "-ac", "1", "-ar", "8000", dst8], capture_output=True, text=True)
print("conv1 rc", r1.returncode, "exists", os.path.exists(dst))
print("conv2 rc", r2.returncode, "exists", os.path.exists(dst8))
if not os.path.exists(dst):
    print("FFMPEG STDERR:\n", r1.stderr[-800:])
    raise SystemExit(1)


def stats(path):
    w = wave.open(path, "rb")
    sr = w.getframerate(); n = w.getnframes()
    d = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float64)
    w.close()
    peak = float(np.max(np.abs(d))) if d.size else 0
    rms = float(np.sqrt(np.mean(d ** 2))) if d.size else 0
    clip = float(np.mean(np.abs(d) > 32000) * 100) if d.size else 0
    print(f"--- {os.path.basename(path)} sr={sr} dur={n/sr:.1f}s")
    print(f"    peak={peak:.0f} rms={rms:.0f} clip%={clip:.2f}")
    return sr, d


for f in (dst, dst8):
    stats(f)

# frequency content via Welch on 8k version
sr, d = stats(dst8)
if d.size:
    d = d / (np.max(np.abs(d)) + 1e-9)
    # simple periodogram in bands
    N = 4096
    win = np.hanning(N)
    acc = np.zeros(N // 2 + 1)
    cnt = 0
    for i in range(0, len(d) - N, N // 2):
        seg = d[i:i + N] * win
        sp = np.abs(np.fft.rfft(seg)) ** 2
        acc += sp; cnt += 1
    if cnt:
        acc /= cnt
        freqs = np.fft.rfftfreq(N, 1.0 / sr)
        bands = [(0, 100), (100, 300), (300, 1000), (1000, 2000), (2000, 3400), (3400, 4000)]
        total = acc.sum() + 1e-12
        print("Band energy % (8k):")
        for lo, hi in bands:
            m = (freqs >= lo) & (freqs < hi)
            print(f"   {lo:>4}-{hi:<4}Hz : {acc[m].sum()/total*100:5.1f}%")
print("ANALYSIS_DONE")
