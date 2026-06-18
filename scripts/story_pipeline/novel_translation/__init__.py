"""Multi-pass novel translation pipeline.

Pass order:
  1. Segmenter   — stable line IDs, risk scoring
  2. Resolver    — pronoun/speaker resolution (high-risk chunks only)
  3. Translator  — faithful translation draft
  4. QA          — deterministic + LLM consistency check
  5. Patcher     — apply approved patches
  6. Polisher    — constrained style polish
  7. FinalQA     — holistic chapter-level review
"""
