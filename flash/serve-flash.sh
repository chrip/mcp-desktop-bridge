#!/bin/bash
# Start nvidia/Qwen3.8-Flash-Next-NVFP4 (the "flash" model), via the blazux recipe
# (~/localAI/qwen3.8-Flash-DGX, container "qwen38-flash", host port 18300).
#
# Parameter choices (2026-10-22):
#   MODE=nvfp4     NVIDIA's checkpoint exactly as published. The recipe's faster
#                  "hybrid" layout rewrites the dense side layers to fp8 with a community
#                  script (+~20% decode); we prefer NVIDIA's own weights unmodified.
#   CTX=163840     matches opencode's limit.context for this model (compaction at ~131k).
#                  No YaRN: Qwen notes static YaRN can hurt short-context quality.
#   GPU_MEM=0.75   ~91 GiB. Measured 2026-10-22: at 0.70 weights + overhead left only
#                  0.35 GiB of KV, while 163840 tokens need 4.91 GiB (bf16), so ~0.745 is
#                  the floor. The rest of the box (desktop session, Nextcloud/Euro-Office
#                  dev stack, gui desktop) uses ~18 GiB, leaving ~12 GiB free; the recipe's
#                  0.80 long-run setting would leave almost nothing.
#   MTP=2          the recipe's quality-first default (MTP=3: +7% decode, -1 tournament point).
#   Served names: "qwen3.8-flash-next" is the real name -- clients (opencode) use it, and it
#                  ends up in Assisted-by trailers, so it must name the actual model version.
#                  "qwen35-flash-next" is only an alias for the desktop bridge: cua picks its
#                  Qwen vision loop with the regex r"(?i).*qwen35.*". Same pattern as the 27B
#                  (qwen3.8-27b + qwen35-27b). serve.sh accepts one name, so the full list is
#                  passed again via EXTRA, which comes later on the command line and wins.
set -euo pipefail
RECIPE="${RECIPE:-$HOME/localAI/qwen3.8-Flash-DGX}"
export HF_CACHE="${HF_CACHE:-$HOME/hf-cache/huggingface}"
exec "$RECIPE/flash" serve native \
  MODEL=nvidia/Qwen3.8-Flash-Next-NVFP4 \
  MODE=nvfp4 \
  CTX="${CTX:-163840}" \
  GPU_MEM="${GPU_MEM:-0.75}" \
  MTP="${MTP:-2}" \
  SERVED_MODEL_NAME=qwen3.8-flash-next \
  EXTRA="--served-model-name qwen3.8-flash-next qwen35-flash-next" \
  PORT=18300
