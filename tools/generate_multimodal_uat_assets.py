"""Generate deterministic, non-sensitive multimodal UAT fixtures and a manifest."""
from __future__ import annotations

import hashlib
import json
import math
import wave
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy.signal import resample_poly
import win32com.client


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "test-assets" / "multimodal"
FONT = Path(r"C:\Windows\Fonts\msyh.ttc")


def font(size: int):
    return ImageFont.truetype(str(FONT), size)


def image_asset() -> Path:
    canvas = Image.new("RGB", (1920, 1080), "#f5f7f1")
    draw = ImageDraw.Draw(canvas)
    draw.text((90, 70), "智能渠道调度多模态测试", fill="#17351f", font=font(68))
    draw.text((90, 160), "Multimodal UAT Test", fill="#3e5d46", font=font(46))
    for x, color, shape in [(180, "#d84242", "circle"), (470, "#2f9f62", "square"), (760, "#3578c9", "triangle")]:
        if shape == "circle": draw.ellipse((x, 310, x + 180, 490), fill=color)
        elif shape == "square": draw.rounded_rectangle((x, 310, x + 180, 490), 22, fill=color)
        else: draw.polygon([(x + 90, 300), (x, 490), (x + 180, 490)], fill=color)
    draw.text((1050, 320), "2026", fill="#15251b", font=font(112))
    chart = [(1120, 820), (1250, 710), (1390, 760), (1530, 590), (1700, 640)]
    draw.line(chart, fill="#2672a8", width=16, joint="curve")
    for x, y in chart: draw.ellipse((x-14, y-14, x+14, y+14), fill="#17351f")
    draw.rectangle((90, 720, 820, 970), fill="#18261d")
    for index in range(8):
        shade = 55 + index * 24
        draw.rectangle((900 + index * 100, 900, 990 + index * 100, 990), fill=(shade, shade, shade))
    path = OUT / "test-image-1920x1080.png"
    canvas.save(path, optimize=True)
    return path


def audio_asset() -> Path:
    raw = OUT / ".sapi-source.wav"
    stream = win32com.client.Dispatch("SAPI.SpFileStream")
    stream.Format.Type = 22  # SAFT22kHz16BitMono
    stream.Open(str(raw), 3, False)
    voice = win32com.client.Dispatch("SAPI.SpVoice")
    voice.AudioOutputStream = stream
    voice.Rate = -1
    voice.Speak("这是智能渠道调度系统的音频测试。测试编号二零二六零八零六。请识别日期、数字和主要内容。")
    stream.Close()
    with wave.open(str(raw), "rb") as source:
        rate, width, channels = source.getframerate(), source.getsampwidth(), source.getnchannels()
        samples = np.frombuffer(source.readframes(source.getnframes()), dtype=np.int16)
    if channels > 1: samples = samples.reshape(-1, channels).mean(axis=1).astype(np.int16)
    samples = resample_poly(samples.astype(np.float64), 16000, rate).clip(-32768, 32767).astype(np.int16)
    target = 16000 * 10
    samples = np.pad(samples[:target], (0, max(0, target - len(samples))))
    path = OUT / "test-audio-10s.wav"
    with wave.open(str(path), "wb") as target_file:
        target_file.setnchannels(1); target_file.setsampwidth(width); target_file.setframerate(16000)
        target_file.writeframes(samples.tobytes())
    raw.unlink(missing_ok=True)
    return path


def video_asset() -> tuple[Path, str]:
    path = OUT / "test-video-1080p.mp4"
    fps, seconds = 30, 9
    selected = ""
    writer = None
    for codec in ("avc1", "H264", "mp4v"):
        candidate = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*codec), fps, (1920, 1080))
        if candidate.isOpened(): writer, selected = candidate, codec; break
        candidate.release()
    if writer is None: raise RuntimeError("no_supported_mp4_encoder")
    for frame_index in range(fps * seconds):
        t = frame_index / fps
        base = np.zeros((1080, 1920, 3), dtype=np.uint8)
        base[:] = (242, 246, 238) if t < 4.5 else (34, 48, 40)
        x = int(100 + (1600 * t / seconds))
        cv2.rectangle(base, (x, 360), (x + 220, 580), (55, 125, 220), -1)
        cv2.circle(base, (1720 - x // 2, 700), 110, (70, 180, 80), -1)
        pil = Image.fromarray(cv2.cvtColor(base, cv2.COLOR_BGR2RGB))
        draw = ImageDraw.Draw(pil)
        title = "智能渠道调度多模态测试" if t < 4.5 else "场景二：动态路由"
        draw.text((90, 100), title, fill="#17351f" if t < 4.5 else "white", font=font(64))
        draw.text((90, 190), "Multimodal UAT Test · 2026", fill="#446b4d" if t < 4.5 else "#b7d7bd", font=font(42))
        writer.write(cv2.cvtColor(np.asarray(pil), cv2.COLOR_RGB2BGR))
    writer.release()
    return path, selected


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""): digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    image_path = image_asset()
    audio_path = audio_asset()
    video_path, codec = video_asset()
    capture = cv2.VideoCapture(str(video_path))
    manifest = {
        "schema_version": "multimodal_uat_assets_v1",
        "assets": [
            {"path": image_path.name, "mime": "image/png", "width": 1920, "height": 1080,
             "bytes": image_path.stat().st_size, "sha256": sha256(image_path)},
            {"path": audio_path.name, "mime": "audio/wav", "sample_rate": 16000,
             "channels": 1, "duration_seconds": 10.0, "bytes": audio_path.stat().st_size,
             "sha256": sha256(audio_path)},
            {"path": video_path.name, "mime": "video/mp4", "width": int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)),
             "height": int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)), "fps": capture.get(cv2.CAP_PROP_FPS),
             "duration_seconds": capture.get(cv2.CAP_PROP_FRAME_COUNT) / capture.get(cv2.CAP_PROP_FPS),
             "codec": codec, "bytes": video_path.stat().st_size, "sha256": sha256(video_path)},
        ],
    }
    capture.release()
    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == "__main__": main()
