"""
embedder.py

Embedding wrapper for Qwen3-VL-Embedding-8B (text-only paths).

Constructor flags
-----------------
model_path : str
    Path to the model directory.  Defaults to ``config.EMBED_MODEL_PATH``
    (which reads ``KNESSET_EMBED_MODEL`` from the environment).

use_cuda : bool
    True  → device_map="auto": GPU first, spill to CPU RAM.
    False → all layers on CPU.

quantize : None | "int8" | "int4"
    None   → bfloat16  (~16 GB weight footprint)
    "int8" → LLM.int8() via bitsandbytes  (~8 GB)
    "int4" → NF4 via bitsandbytes          (~4 GB, recommended when llama-server
              is running simultaneously — fits in the leftover VRAM)

Recommended presets
-------------------
Indexing (llama-server OFF, full GPU free):
    ProtocolEmbedder(use_cuda=True)

Query time (llama-server ON, GPU shared with a 3060):
    ProtocolEmbedder(use_cuda=True, quantize="int4")

Requirements
------------
    pip install transformers>=4.57.0 qwen-vl-utils>=0.0.14 torch
    pip install bitsandbytes   # only needed for quantize="int8"/"int4"
"""

import sys
from pathlib import Path
from typing import List

import numpy as np
import torch
import torch.nn.functional as F

import config


class ProtocolEmbedder:
    # Symmetric instruction — bullets and speeches embedded in the same space.
    INSTR_ASSIGN     = "Represent a Knesset committee parliamentary discussion topic."
    # Asymmetric document instructions.
    INSTR_BULLET_DOC = "Represent a Knesset committee meeting topic or decision for retrieval."
    INSTR_DIALOG_DOC = "Represent a Knesset committee parliamentary discussion for retrieval."
    # Query instruction (retrieval time).
    INSTR_QUERY      = "Retrieve Knesset committee meeting information about this political question."

    def __init__(
        self,
        model_path: str | None = None,
        use_cuda: bool = True,
        quantize: str | None = None,   # None | "int8" | "int4"
        batch_size: int = config.EMBED_BATCH_SIZE,
    ):
        self.batch_size = batch_size
        model_path = model_path or config.EMBED_MODEL_PATH

        # Inject the model's bundled scripts into sys.path so the custom
        # Qwen3VLForEmbedding class can be imported.
        scripts_dir = str(Path(model_path) / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)

        from qwen3_vl_embedding import Qwen3VLForEmbedding          # noqa: PLC0415
        from transformers.models.qwen3_vl.processing_qwen3_vl import (  # noqa: PLC0415
            Qwen3VLProcessor,
        )

        load_kwargs: dict = {"trust_remote_code": True}

        if quantize == "int8":
            load_kwargs["load_in_8bit"] = True
        elif quantize == "int4":
            load_kwargs["load_in_4bit"] = True
            load_kwargs["bnb_4bit_compute_dtype"] = torch.float16
            load_kwargs["bnb_4bit_quant_type"]    = "nf4"
        else:
            load_kwargs["dtype"] = torch.bfloat16

        load_kwargs["device_map"] = "auto" if use_cuda else {"": "cpu"}

        mode = f"{'cuda' if use_cuda else 'cpu'} / {quantize or 'bfloat16'}"
        print(f"[embedder] loading Qwen3-VL-Embedding-8B  ({mode}) …")

        self.model = Qwen3VLForEmbedding.from_pretrained(model_path, **load_kwargs)
        self.model.eval()

        self.processor = Qwen3VLProcessor.from_pretrained(
            model_path, padding_side="right"
        )

        if use_cuda and torch.cuda.is_available():
            self._input_device = torch.device("cuda")
        else:
            self._input_device = torch.device("cpu")

        print(f"[embedder] ready  (input → {self._input_device}, quantize={quantize})")

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _conversations(self, texts: List[str], instruction: str) -> list:
        return [
            [
                {"role": "system", "content": [{"type": "text", "text": instruction}]},
                {"role": "user",   "content": [{"type": "text", "text": t}]},
            ]
            for t in texts
        ]

    # ── Public API ─────────────────────────────────────────────────────────────

    @torch.no_grad()
    def embed(
        self,
        texts: List[str],
        instruction: str,
        batch_size: int | None = None,
    ) -> np.ndarray:
        """
        Embed a list of strings.

        Returns a float32 numpy array of shape (N, D), L2-normalised so that
        cosine similarity == dot product.
        """
        bs = batch_size or self.batch_size
        all_embs: list[np.ndarray] = []

        for i in range(0, len(texts), bs):
            batch = texts[i : i + bs]
            convs = self._conversations(batch, instruction)

            raw    = self.processor.apply_chat_template(
                convs, add_generation_prompt=True, tokenize=False
            )
            inputs = self.processor(
                text=raw,
                padding=True,
                truncation=True,
                max_length=8192,
                return_tensors="pt",
            )
            inputs = {k: v.to(self._input_device) for k, v in inputs.items()}

            out    = self.model(**inputs)
            # Cast to float32 before CPU transfer — quantized models may output
            # float16/bfloat16, and numpy doesn't support bfloat16.
            hidden = out.last_hidden_state.to(torch.float32).cpu()   # (B, seq, D)
            mask   = inputs["attention_mask"].cpu()                   # (B, seq)

            # Last-token pooling (mirrors Qwen3VLEmbedder._pooling_last)
            last_pos = mask.flip(dims=[1]).argmax(dim=1)
            col      = mask.shape[1] - last_pos - 1
            row      = torch.arange(hidden.shape[0])
            embs     = hidden[row, col]                               # (B, D)
            embs     = F.normalize(embs, p=2, dim=-1)

            all_embs.append(embs.numpy())

        return np.vstack(all_embs) if all_embs else np.zeros((0, 4096), dtype=np.float32)
