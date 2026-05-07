import base64
import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY") or os.getenv("openai_api_key"))

OPENAI_IMAGE_MODEL = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1-mini")
SUPPORTED_TYPES = {".jpg", ".jpeg", ".png", ".webp"}


def edit_image(input_path, output_path, prompt, size="1024x1024"):
    input_path = Path(input_path)
    output_path = Path(output_path)

    if not input_path.exists():
        raise FileNotFoundError(f"Image not found: {input_path}")

    if input_path.suffix.lower() not in SUPPORTED_TYPES:
        raise ValueError(f"Unsupported image type: {input_path.suffix}")

    with input_path.open("rb") as image_file:
        result = client.images.edit(
            model=OPENAI_IMAGE_MODEL,
            image=image_file,
            prompt=prompt,
            size=size,
        )

    image_bytes = base64.b64decode(result.data[0].b64_json)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(image_bytes)

    print(f"Saved {output_path}")


async def genderswap_wrap(input_path, output_path="gender_swap.png"):
    prompt = """
Transform the person into the opposite gender while keeping them clearly recognisable as the same individual.

Make realistic, subtle adjustments:
- For male → female: soften facial features, remove facial hair, adjust hairstyle, refine skin texture, natural makeup if appropriate.
- For female → male: sharpen jawline slightly, adjust brow and facial structure subtly, add light facial hair if natural, adjust hairstyle.

Avoid exaggeration, avoid cartoonish features, avoid changing identity.

Keep everything else unchanged: pose, lighting, background, framing, and expression.
Photorealistic result.
"""

    edit_image(
        input_path=input_path,
        output_path=output_path,
        prompt=prompt,
    )
        

