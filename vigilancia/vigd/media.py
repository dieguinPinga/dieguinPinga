"""Operaciones con ffmpeg sobre archivos ya capturados (nunca toca dispositivos)."""
import subprocess

PCM_IN = ["-f", "s16le", "-ar", "16000", "-ac", "1"]


def ffmpeg(args, timeout=180):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"] + args
    p = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg falló ({p.returncode}): {p.stderr.decode(errors='replace')[-600:]}")


def encode_audio(pcm_path, out):
    ffmpeg(PCM_IN + ["-i", pcm_path, "-c:a", "aac", "-b:a", "48k", "-movflags", "+faststart", out])


def waveform(pcm_path, out):
    ffmpeg(PCM_IN + ["-i", pcm_path, "-filter_complex",
                     "showwavespic=s=320x180:colors=0x4fc3f7", "-frames:v", "1", out])


def build_clip(list_path, out, ss, dur, pcm_path=None, audio_offset=0.0):
    """Concatena segmentos sin recodificar; agrega el audio del evento alineado por reloj."""
    args = ["-f", "concat", "-safe", "0", "-ss", f"{ss:.2f}", "-t", f"{dur:.2f}", "-i", list_path]
    if pcm_path:
        if audio_offset >= 0:
            args += ["-itsoffset", f"{audio_offset:.3f}"] + PCM_IN + ["-i", pcm_path]
        else:
            args += ["-ss", f"{-audio_offset:.3f}"] + PCM_IN + ["-i", pcm_path]
        args += ["-map", "0:v:0", "-map", "1:a:0", "-c:a", "aac", "-b:a", "48k"]
    else:
        args += ["-map", "0:v:0"]
    ffmpeg(args + ["-c:v", "copy", "-movflags", "+faststart", out])


def thumbnail(video, at, out):
    ffmpeg(["-ss", f"{max(0.0, at):.2f}", "-i", video, "-frames:v", "1",
            "-vf", "scale=320:-2", "-q:v", "5", out])


def last_frame(segment, out):
    ffmpeg(["-sseof", "-1", "-i", segment, "-frames:v", "1", "-q:v", "4", "-update", "1", out], timeout=20)


def concat_list(path, segment_paths):
    with open(path, "w") as f:
        for p in segment_paths:
            f.write("file '" + p.replace("'", "'\\''") + "'\n")
