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
Optional:
  REFERENCE_IMAGE_PATH - path to a saved reference image to reuse across ALL videos
                          (recommended once you like a design — see note at bottom of file)
  TTS_VOICE             - edge-tts voice name (default: en-US-GuyNeural)
"""

import asyncio
import os
import re
import subprocess
import time
from pathlib import Path

import requests

BASE_URL = "https://apihub.agnes-ai.com"
AGNES_API_KEY = os.environ["AGNES_API_KEY"]
HEADERS = {"Authorization": f"Bearer {AGNES_API_KEY}", "Content-Type": "application/json"}

REFERENCE_IMAGE_PATH = os.environ.get("REFERENCE_IMAGE_PATH")  # optional local file
TTS_VOICE = os.environ.get("TTS_VOICE", "en-US-AvaMultilingualNeural")

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


def split_script(path: Path) -> list[dict]:
    """Split the script into scenes. Every '.' marks the end of one scene/point.

    You can add a visual direction in square brackets right before the full
    stop, e.g.:
        A hiker was stranded on a mountain ledge [snowy cliff, night, dramatic lighting].
    The bracketed part is used only for the image/video prompt — it is
    stripped out before narration, so it never gets spoken aloud.
    """
    text = path.read_text(encoding="utf-8")
    raw_sentences = [s.strip() for s in text.split(".") if s.strip()]

    scenes = []
    for raw in raw_sentences:
        match = re.search(r"\[(.*?)\]", raw)
        visual = match.group(1).strip() if match else ""
        narration = re.sub(r"\[.*?\]", "", raw).strip()
        scenes.append({"text": narration, "visual": visual})
    return scenes


def upload_local_image(path: str) -> str:
    """Agnes needs a public URL for image inputs. If you keep a local reference
    image, host it somewhere reachable (e.g. commit it to a public repo path,
    or push it to any free static host) and return that URL here instead."""
    raise NotImplementedError(
        "Point this at wherever your reference image is publicly hosted, "
        "or just call generate_character_reference() fresh each run."
    )


def generate_character_reference(scenes: list[dict]) -> str:
    """Generate a character reference based on the day's script, so the
    person/subject shown matches whoever the incident is actually about."""
    if REFERENCE_IMAGE_PATH:
        return upload_local_image(REFERENCE_IMAGE_PATH)

    first = scenes[0]
    detail = f" {first['visual']}." if first["visual"] else ""
    prompt = (
        f"Anime style character design, front-facing, clean cel-shaded anime art. "
        f"Depict the main person in this real event: {first['text']}.{detail}"
    )
    payload = {
        "model": "agnes-image-2.1-flash",
        "prompt": prompt,
        "size": IMAGE_SIZE,
        "extra_body": {"response_format": "url"},
    }
    r = requests.post(f"{BASE_URL}/v1/images/generations", headers=HEADERS, json=payload, timeout=120)
    r.raise_for_status()
    return r.json()["data"][0]["url"]


def generate_scene_image(scene: dict, reference_url: str) -> str:
    """Generate a scene image that reuses the reference so the character matches."""
    detail = f" Visual direction: {scene['visual']}." if scene["visual"] else ""
    prompt = (
        f"Same character as the reference image, same face, hairstyle and outfit. "
        f"Scene: {scene['text']}.{detail} Anime style, consistent character design, cinematic framing."
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


def create_video_task(image_url: str, scene: dict, seed: int = 42) -> str:
    detail = f" {scene['visual']}." if scene["visual"] else ""
    payload = {
        "model": "agnes-video-v2.0",
        "prompt": f"{scene['text']}.{detail} Subtle natural motion, keep character face and outfit identical.",
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

        # Agnes sometimes wraps the task status under a "data" key, sometimes not.
        status_obj = data.get("data") or data
        status = status_obj.get("status")

        if status == "completed":
            # The finished video URL has shown up under several different field
            # names in practice — check all of them rather than trusting one.
            url = (
                status_obj.get("video_url")
                or status_obj.get("url")
                or status_obj.get("remixed_from_video_id")
                or (status_obj.get("metadata") or {}).get("url")
                or data.get("result_url")
            )
            if not url:
                raise RuntimeError(f"Video completed but no URL field found. Raw response: {data}")
            return url

        if status == "failed":
            raise RuntimeError(f"Video generation failed: {status_obj.get('error')}")

        time.sleep(interval)
    raise TimeoutError(f"Video {video_id} timed out")


def download(url: str, path: Path) -> None:
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    path.write_bytes(r.content)


CAPTION_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def merge_clip_with_caption(video_path: Path, audio_path: Path, caption_text: str, out_path: Path) -> None:
    """Combine a scene's video + narration audio, and burn in the caption:
    white bold text with a black outline, centered near the bottom."""
    caption_file = OUTPUT_DIR / "caption.txt"
    caption_file.write_text(caption_text, encoding="utf-8")

    drawtext = (
        f"drawtext=fontfile={CAPTION_FONT}:textfile={caption_file}:"
        f"fontcolor=white:fontsize=56:borderw=4:bordercolor=black:"
        f"x=(w-text_w)/2:y=h-th-100:line_spacing=10"
    )

    subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", str(video_path),
            "-i", str(audio_path),
            "-vf", drawtext,
            "-c:v", "libx264", "-c:a", "aac", "-shortest",
            str(out_path),
        ],
        check=True,
    )


def generate_voiceover(text: str, out_path: Path) -> None:
    import edge_tts

    async def run():
        communicate = edge_tts.Communicate(text, TTS_VOICE)
        await communicate.save(str(out_path))

    asyncio.run(run())


def upload_to_youtube(video_path: Path, scenes: list[dict]) -> str:
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
    title = scenes[0]["text"][:90].strip()
    if "#shorts" not in title.lower():
        title = f"{title} #Shorts"

    full_script = ". ".join(s["text"] for s in scenes) + "."
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
    scenes = split_script(SCRIPT_PATH)
    print(f"Found {len(scenes)} scenes in script.txt")

    reference_url = generate_character_reference(scenes)
    print(f"Character reference: {reference_url}")

    merged_clips = []
    for i, scene in enumerate(scenes):
        print(f"\nScene {i + 1}/{len(scenes)}: {scene['text']}")
        if scene["visual"]:
            print(f"  Visual direction: {scene['visual']}")

        scene_image_url = generate_scene_image(scene, reference_url)
        video_id = create_video_task(scene_image_url, scene)
        video_url = poll_video(video_id)

        clip_path = OUTPUT_DIR / f"scene_{i:02d}.mp4"
        download(video_url, clip_path)

        audio_path = OUTPUT_DIR / f"scene_{i:02d}.mp3"
        generate_voiceover(scene["text"], audio_path)

        merged_path = OUTPUT_DIR / f"merged_{i:02d}.mp4"
        merge_clip_with_caption(clip_path, audio_path, scene["text"], merged_path)
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
        upload_to_youtube(final_output, scenes)
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
