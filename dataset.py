"""
Synthetic multimodal dataset for development and testing.

Replace SyntheticMultimodalDataset with your real data loader;
the train.py / infer.py interfaces remain identical.
"""

from __future__ import annotations

import torch
from torch.utils.data import Dataset, DataLoader, random_split
from config import TrainConfig


class SyntheticMultimodalDataset(Dataset):
    """
    Generates random (image, tokens, label) triples entirely in memory.

    image  : float32 tensor [3, image_size, image_size]  — pixel values in [0, 1]
    tokens : int64 tensor   [seq_len]                    — random token ids
    label  : int64 scalar   in {0, …, num_classes - 1}
    """

    def __init__(
        self,
        num_samples: int,
        image_size: int,
        seq_len: int,
        vocab_size: int,
        num_classes: int,
        seed: int = 42,
    ) -> None:
        super().__init__()
        rng = torch.Generator().manual_seed(seed)

        self.images = torch.rand(num_samples, 3, image_size, image_size, generator=rng)
        self.tokens = torch.randint(
            1, vocab_size, (num_samples, seq_len), generator=rng
        )
        self.labels = torch.randint(0, num_classes, (num_samples,), generator=rng)

    def __len__(self) -> int:
        return len(self.labels)

    def __getitem__(self, idx: int):
        return self.images[idx], self.tokens[idx], self.labels[idx]


def build_dataloaders(
    cfg: TrainConfig,
    model_cfg,
    val_fraction: float = 0.15,
    num_workers: int = 0,
) -> tuple[DataLoader, DataLoader]:
    """
    Return (train_loader, val_loader) from a synthetic dataset.

    Args:
        cfg          : TrainConfig
        model_cfg    : ModelConfig — needed for vocab_size, num_classes
        val_fraction : fraction of data used for validation
        num_workers  : DataLoader worker processes (0 = main process only)
    """
    dataset = SyntheticMultimodalDataset(
        num_samples=cfg.num_samples,
        image_size=cfg.image_size,
        seq_len=cfg.seq_len,
        vocab_size=model_cfg.vocab_size,
        num_classes=model_cfg.num_classes,
    )

    val_size = max(1, int(len(dataset) * val_fraction))
    train_size = len(dataset) - val_size
    train_ds, val_ds = random_split(
        dataset,
        [train_size, val_size],
        generator=torch.Generator().manual_seed(0),
    )

    # pin_memory is beneficial on CUDA but unsupported on MPS/CPU
    pin = torch.cuda.is_available()

    train_loader = DataLoader(
        train_ds,
        batch_size=cfg.batch_size,
        shuffle=True,
        num_workers=num_workers,
        pin_memory=pin,
    )
    val_loader = DataLoader(
        val_ds,
        batch_size=cfg.batch_size,
        shuffle=False,
        num_workers=num_workers,
        pin_memory=pin,
    )
    return train_loader, val_loader
