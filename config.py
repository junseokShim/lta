"""
Project-wide configuration dataclass.
Edit values here or pass overrides via CLI flags in train.py / infer.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ModelConfig:
    # Image branch
    image_hidden: int = 512
    image_out: int = 256

    # Text branch
    vocab_size: int = 10_000
    embed_dim: int = 128
    text_hidden: int = 256
    text_out: int = 256

    # Fusion head
    fusion_hidden: int = 256
    num_classes: int = 10

    dropout: float = 0.3


@dataclass
class TrainConfig:
    epochs: int = 20
    batch_size: int = 32
    learning_rate: float = 1e-3
    weight_decay: float = 1e-4

    # Data
    num_samples: int = 2_000   # synthetic dataset size
    image_size: int = 32       # H × W per image
    seq_len: int = 16          # tokens per text sample

    # Checkpointing
    checkpoint_dir: Path = field(default_factory=lambda: Path("checkpoints"))
    save_every: int = 5        # epochs between saves

    # Device — "auto" resolves CUDA → MPS → CPU automatically
    device: str = "auto"


@dataclass
class InferConfig:
    checkpoint: Path = field(default_factory=lambda: Path("checkpoints/best.pt"))
    device: str = "auto"
    batch_size: int = 16
