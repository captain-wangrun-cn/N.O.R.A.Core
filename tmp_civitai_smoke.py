"""一次性冒烟脚本：拿真 token 打真 Civitai，验证 editImage 请求是否有效。"""

import asyncio
import base64
import json
import sys

import config

TOKEN = "acf6b01c198b59a65988c41f0a78b71f"
PROXY = "http://127.0.0.1:7890"
IMG = r"D:\Downloads\380db4334731536ff87731f6dc90be1c.jpg"
PROMPT = "change the white stockings to black stockings"
MODEL = sys.argv[1] if len(sys.argv) > 1 else "grok"
OUT = sys.argv[2] if len(sys.argv) > 2 else r"D:\Downloads\civitai_out.jpg"

_opts = {"proxy": PROXY, "engine": None}
config.get_model_provider = lambda alias="draw": "civitai_main"
config.get_api_key = lambda provider=None: TOKEN
config.get_base_url = lambda provider=None: "https://orchestration.civitai.com"
config.get_model_name = lambda alias="draw": MODEL
config.get_provider_option = lambda provider=None, key="": _opts.get(key)

from brain.providers.civitai import CivitaiProvider


async def main():
    raw = open(IMG, "rb").read()
    ref = {
        "mime_type": "image/jpeg",
        "bytes": raw,
        "base64": base64.b64encode(raw).decode(),
    }
    p = CivitaiProvider(model_alias="draw")

    body = {"steps": [{"$type": "imageGen", "input": p._build_input(PROMPT, ["<data-uri>"])}]}
    print("MODEL:", MODEL)
    print("REQUEST:", json.dumps(body, ensure_ascii=False))

    try:
        out = await p.generate_image(PROMPT, reference_images=[ref])
    except Exception as e:
        print("FAILED:", type(e).__name__, str(e)[:1500])
        return

    open(OUT, "wb").write(out["bytes"])
    print("OK:", out["mime_type"], len(out["bytes"]), "bytes ->", OUT)


asyncio.run(main())
