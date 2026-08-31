"""
Daily YouTube Shorts pipeline.

- Reads script.txt (one script per day, written by hand).
- Splits it into scenes: every sentence ending in a full stop becomes one scene.
- Generates one character reference image, then a consistent scene image per sentence
  (image-to-image against the reference, so the character stays the same).
- Animates each scene with Agnes Video.
- Adds narration (edge-tts, free) per scene.
- Concatenates everything into output/final_video.mp4.

Env vars required:
  AGNES_API_KEY       - your Agnes AI API key
  CHARACTER_PROMPT     - detailed description of your recurring character (style, face, outfit)
Optional:
  REFERENCE_IMAGE_PATH - path to a saved reference image to reuse across ALL videos
                          (recommended once you like a design — see note at bottom of file)
  TTS_VOICE             - edge-tts voice name (default: en-US-GuyNeural)
"""

import asyncio
import os
import subprocess
import time
from pathlib import Path

import requests

BASE_URL = "https://apihub.agnes-ai.com"
AGNES_API_KEY = os.environ["AGNES_API_KEY"]
HEADERS = {"Authorization": f"Bearer {AGNES_API_KEY}", "Content-Type": "application/json"}

CHARACTER_PROMPT = os.environ.get(
    "CHARACTER_PROMPT",
    "anime style news narrator, young adult, short black hair, dark blue blazer, "
    "neutral studio background, clean cel-shaded anime art style, front-facing",
)
REFERENCE_IMAGE_PATH = os.environ.get("REFERENCE_IMAGE_PATH")  # optional local file
TTS_VOICE = os.environ.get("TTS_VOICE", "en-US-GuyNeural")

# Shorts must be vertical 9:16 to land on the Shorts shelf.
IMAGE_SIZE = "768x1152"       # portrait, close to 2:3 — good source for 9:16 video
VIDEO_WIDTH = 768
VIDEO_HEIGHT = 1152

# Hashtags added to every video's description automatically.
# Edit this list to whatever fits your niche — keep it to ~3-5, more gets ignored.
DEFAULT_HASHTAGS = ["#Shorts", "#news", "#truecrime", "#reallife"]

SCRIPT_PATH = Path("script.txt")
OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)


def split_script(path: Path) -> list[str]:
    """Split the script into scenes. Every '.' marks the end of one scene/point."""
    text = path.read_text(encoding="utf-8")
    sentences = [s.strip() for s in text.split(".") if s.strip()]
    return sentences


def upload_local_image(path: str) -> str:
    """Agnes needs a public URL for image inputs. If you keep a local reference
    image, host it somewhere reachable (e.g. commit it to a public repo path,
    or push it to any free static host) and return that URL here instead."""
    raise NotImplementedError(
        "Point this at wherever your reference image is publicly hosted, "
        "or just call generate_character_reference() fresh each run."
    )


def generate_character_reference() -> str:
    """Generate a fresh character reference image and return its URL."""
    if REFERENCE_IMAGE_PATH:
        return upload_local_image(REFERENCE_IMAGE_PATH)

    payload = {
        "model": "agnes-image-2.1-flash",
        "prompt": CHARACTER_PROMPT,
        "size": IMAGE_SIZE,
        "extra_body": {"response_format": "url"},
    }
    r = requests.post(f"{BASE_URL}/v1/images/generations", headers=HEADERS, json=payload, timeout=120)
    r.raise_for_status()
    return r.json()["data"][0]["url"]


def generate_scene_image(sentence: str, reference_url: str) -> str:
    """Generate a scene image that reuses the reference so the character matches."""
    prompt = (
        f"Same character as the reference image, same face, hairstyle and outfit. "
        f"Scene: {sentence}. Anime style, consistent character design, cinematic framing."
    )
    payload = {
        "model": "agnes-image-2.1-flash",
        "prompt": prompt,
        "image": [reference_url],
        "size": IMAGE_SIZE,
        "extra_body": {"response_format": "url"},
    }
    r = requests.post(f"{BASE_URL}/v1/images/generations", headers=HEADERS, json=payload, timeout=120)
    r.raise_for_status()
    return r.json()["data"][0]["url"]


def create_video_task(image_url: str, sentence: str, seed: int = 42) -> str:
    payload = {
        "model": "agnes-video-v2.0",
        "prompt": f"{sentence}. Subtle natural motion, keep character face and outfit identical.",
        "image": image_url,
        "width": VIDEO_WIDTH,
        "height": VIDEO_HEIGHT,
        "num_frames": 81,   # ~3 seconds at 24fps; raise if a sentence needs more screen time
        "frame_rate": 24,
        "seed": seed,
    }
    r = requests.post(f"{BASE_URL}/v1/videos", headers=HEADERS, json=payload, timeout=60)
    r.raise_for_status()
    return r.json()["video_id"]


def poll_video(video_id: str, timeout: int = 300, interval: int = 5) -> str:
    start = time.time()
    while time.time() - start < timeout:
        r = requests.get(f"{BASE_URL}/agnesapi", headers=HEADERS, params={"video_id": video_id}, timeout=30)
        r.raise_for_status()
        data = r.json()
        if data["status"] == "completed":
            return data["metadata"]["url"]
        if data["status"] == "failed":
            raise RuntimeError(f"Video generation failed: {data.get('error')}")
        time.sleep(interval)
    raise TimeoutError(f"Video {video_id} timed out")


def download(url: str, path: Path) -> None:
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    path.write_bytes(r.content)


def generate_voiceover(sentence: str, out_path: Path) -> None:
    import edge_tts

    async def run():
        communicate = edge_tts.Communicate(sentence, TTS_VOICE)
        await communicate.save(str(out_path))

    asyncio.run(run())


def upload_to_youtube(video_path: Path, sentences: list[str]) -> str:
    """Upload the finished video as a public YouTube Short, with default hashtags."""
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds = Credentials(
        None,
        refresh_token=os.environ["YOUTUBE_REFRESH_TOKEN"],
        client_id=os.environ["YOUTUBE_CLIENT_ID"],
        client_secret=os.environ["YOUTUBE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
    )
    youtube = build("youtube", "v3", credentials=creds)

    # Title: first sentence, trimmed to leave room for "#Shorts" (100 char limit total).
    title = sentences[0][:90].strip()
    if "#shorts" not in title.lower():
        title = f"{title} #Shorts"

    full_script = ". ".join(sentences) + "."
    hashtag_line = " ".join(DEFAULT_HASHTAGS)
    description = f"{full_script}\n\n{hashtag_line}"

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": [h.lstrip("#") for h in DEFAULT_HASHTAGS],
            "categoryId": "25",  # News & Politics — change if your niche fits another category better
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(str(video_path), mimetype="video/mp4", resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        status, response = request.next_chunk()
    video_id = response["id"]
    print(f"Uploaded: https://youtube.com/shorts/{video_id}")
    return video_id


def main() -> None:
    sentences = split_script(SCRIPT_PATH)
    print(f"Found {len(sentences)} scenes in script.txt")

    reference_url = generate_character_reference()
    print(f"Character reference: {reference_url}")

    merged_clips = []
    for i, sentence in enumerate(sentences):
        print(f"\nScene {i + 1}/{len(sentences)}: {sentence}")

        scene_image_url = generate_scene_image(sentence, reference_url)
        video_id = create_video_task(scene_image_url, sentence)
        video_url = poll_video(video_id)

        clip_path = OUTPUT_DIR / f"scene_{i:02d}.mp4"
        download(video_url, clip_path)

        audio_path = OUTPUT_DIR / f"scene_{i:02d}.mp3"
        generate_voiceover(sentence, audio_path)

        merged_path = OUTPUT_DIR / f"merged_{i:02d}.mp4"
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", str(clip_path),
                "-i", str(audio_path),
                "-c:v", "copy", "-c:a", "aac", "-shortest",
                str(merged_path),
            ],
            check=True,
        )
        merged_clips.append(merged_path)

    concat_list = OUTPUT_DIR / "concat.txt"
    concat_list.write_text("\n".join(f"file '{p.resolve()}'" for p in merged_clips), encoding="utf-8")

    final_output = OUTPUT_DIR / "final_video.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(concat_list),
            "-c", "copy",
            str(final_output),
        ],
        check=True,
    )
    print(f"\nDone: {final_output}")

    if os.environ.get("YOUTUBE_REFRESH_TOKEN"):
        upload_to_youtube(final_output, sentences)
    else:
        print("Skipping upload (no YOUTUBE_REFRESH_TOKEN set) — video saved locally only.")


if __name__ == "__main__":
    main()

# NOTE on character consistency across DAYS, not just within one video:
# Regenerating the reference image from CHARACTER_PROMPT every run gives a
# similar but not pixel-identical character each day. If you want the exact
# same character every single video, generate the reference once, save the
# image file, host it at a stable public URL (a raw GitHub URL to a file in
# this repo works), and set REFERENCE_IMAGE_PATH so every run reuses it
# instead of generating a new one.
