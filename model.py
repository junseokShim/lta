"""
Multimodal prediction model — image + text → class logits.

Architecture
------------
  ImageEncoder  : Conv → Pool → Linear → [B, image_out]
  TextEncoder   : Embedding → GRU → Linear → [B, text_out]
  FusionHead    : Concat → FC → Dropout → FC → logits [B, num_classes]

All sub-modules accept standard torch.Tensor inputs and are device-agnostic;
the caller is responsible for placing data and the model on the right device
(see device.py).
"""

from __future__ import annotations

import torch
import torch.nn as nn
from config import ModelConfig


class ImageEncoder(nn.Module):
    """Encodes a batch of images [B, C, H, W] → [B, image_out]."""

    def __init__(self, cfg: ModelConfig) -> None:
        super().__init__()
        self.net = nn.Sequential(
            # Block 1 — 3 → 32
            nn.Conv2d(3, 32, kernel_size=3, padding=1),
            nn.BatchNorm2d(32),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(2),               # H/2, W/2
            # Block 2 — 32 → 64
            nn.Conv2d(32, 64, kernel_size=3, padding=1),
            nn.BatchNorm2d(64),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(2),               # H/4, W/4
            nn.AdaptiveAvgPool2d((4, 4)),  # fixed spatial size
            nn.Flatten(),                  # 64 * 4 * 4 = 1024
        )
        self.proj = nn.Sequential(
            nn.Linear(64 * 4 * 4, cfg.image_hidden),
            nn.ReLU(inplace=True),
            nn.Dropout(cfg.dropout),
            nn.Linear(cfg.image_hidden, cfg.image_out),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.proj(self.net(x))


class TextEncoder(nn.Module):
    """Encodes a batch of token sequences [B, L] → [B, text_out]."""

    def __init__(self, cfg: ModelConfig) -> None:
        super().__init__()
        self.embed = nn.Embedding(cfg.vocab_size, cfg.embed_dim, padding_idx=0)
        self.gru = nn.GRU(
            cfg.embed_dim,
            cfg.text_hidden,
            num_layers=2,
            batch_first=True,
            dropout=cfg.dropout,
            bidirectional=True,
        )
        self.proj = nn.Linear(cfg.text_hidden * 2, cfg.text_out)

    def forward(self, tokens: torch.Tensor) -> torch.Tensor:
        emb = self.embed(tokens)                       # [B, L, embed_dim]
        _, h = self.gru(emb)                           # h: [4, B, text_hidden]
        # Concat last fwd + bwd hidden state
        h_cat = torch.cat([h[-2], h[-1]], dim=-1)      # [B, text_hidden * 2]
        return self.proj(h_cat)                         # [B, text_out]


class MultimodalPredictor(nn.Module):
    """
    Full multimodal model.

    Inputs
    ------
    images : Tensor [B, 3, H, W]
    tokens : LongTensor [B, L]

    Output
    ------
    logits : Tensor [B, num_classes]
    """

    def __init__(self, cfg: ModelConfig) -> None:
        super().__init__()
        self.image_enc = ImageEncoder(cfg)
        self.text_enc = TextEncoder(cfg)

        fusion_in = cfg.image_out + cfg.text_out
        self.head = nn.Sequential(
            nn.Linear(fusion_in, cfg.fusion_hidden),
            nn.ReLU(inplace=True),
            nn.Dropout(cfg.dropout),
            nn.Linear(cfg.fusion_hidden, cfg.num_classes),
        )

    def forward(self, images: torch.Tensor, tokens: torch.Tensor) -> torch.Tensor:
        img_feat = self.image_enc(images)
        txt_feat = self.text_enc(tokens)
        fused = torch.cat([img_feat, txt_feat], dim=-1)
        return self.head(fused)
