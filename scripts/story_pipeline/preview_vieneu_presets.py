#!/usr/bin/env python3
"""Generate VieNeu v3 preset voice previews for xianxia audiobook A/B listening.

Usage:
  viterbox/venv/bin/python scripts/story_pipeline/preview_vieneu_presets.py
  viterbox/venv/bin/python scripts/story_pipeline/preview_vieneu_presets.py \\
      --profiles preset_trong_huu,preset_duc_tri,xianxia_spirit_male
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("GRADIO_TEMP_DIR", str(Path(tempfile.gettempdir()) / "betterbox_story_tmp"))

from scripts.story_pipeline.vieneu_voice_profiles import (  # noqa: E402
    DEFAULT_VIENEU_VOICE_PROFILE,
    get_vieneu_voice_profile,
    list_vieneu_voice_profiles,
)
from scripts.story_pipeline.vieneu_audiobook_stitch import (  # noqa: E402
    get_vieneu_sample_rate,
    synthesize_vieneu_audiobook,
)

XIANXIA_SAMPLE = (
    "Enkrid nhìn lên bầu trời, linh lực trong kinh mạch chầm chậm lưu chuyển. "
    "Trước mắt là con đường tu luyện dài vô tận, nhưng anh không do dự. "
    "Mỗi bước tiến đều phải trả bằng mồ hôi và ý chí sắt đá."
)

# Top picks for tiên hiệp long-form — presets first (stable speaker tokens).
DEFAULT_COMPARE = (
    "preset_trong_huu",
    "preset_binh_an",
    "preset_duc_tri",
    "preset_gia_bao",
    "xianxia_spirit_male",
    "dolly_steadfast_narrator",
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Preview VieNeu voices for xianxia audiobook.")
    parser.add_argument("--output-dir", default="/tmp/vieneu_preset_preview")
    parser.add_argument("--text", default=XIANXIA_SAMPLE)
    parser.add_argument("--profiles", default=",".join(DEFAULT_COMPARE))
    parser.add_argument("--device", default="auto")
    parser.add_argument("--emotion", default="natural")
    args = parser.parse_args()

    keys = [k.strip() for k in args.profiles.split(",") if k.strip()]
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    from vieneu import Vieneu  # noqa: WPS433

    print(f"[LOAD] VieNeu device={args.device}")
    tts = Vieneu(device=args.device) if args.device != "auto" else Vieneu()
    sr = get_vieneu_sample_rate(tts)

    print(f"[PREVIEW] output={out_dir} current_default={DEFAULT_VIENEU_VOICE_PROFILE}")
    print(f"[TEXT] {args.text[:80]}...")
    print()

    import soundfile as sf

    for key in keys:
        profile = get_vieneu_voice_profile(key)
        if profile is None:
            print(f"[SKIP] unknown profile: {key}")
            continue
        out_path = out_dir / f"{key}.wav"
        try:
            audio = synthesize_vieneu_audiobook(
                tts,
                args.text,
                voice_profile=key,
                emotion=args.emotion,
            )
            sf.write(out_path, audio, sr)
            print(f"[OK] {key:28s} → {out_path} ({profile.label})")
        except Exception as exc:
            print(f"[FAIL] {key}: {exc}")

    print()
    print("Nghe file WAV → chọn profile → set DEFAULT_VIENEU_VOICE_PROFILE + docker env.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
