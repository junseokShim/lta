"""
Training script for MultimodalPredictor.

Usage
-----
python train.py                         # auto device, default config
python train.py --device cuda           # force CUDA
python train.py --device mps            # force Apple Silicon MPS
python train.py --device cpu            # force CPU
python train.py --epochs 30 --lr 5e-4
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Train MultimodalPredictor")
    p.add_argument("--device",     default="auto",  help="cuda | mps | cpu | auto")
    p.add_argument("--epochs",     type=int,   default=None)
    p.add_argument("--batch-size", type=int,   default=None)
    p.add_argument("--lr",         type=float, default=None)
    p.add_argument("--checkpoint-dir", default=None)
    return p.parse_args()


def train() -> None:
    args = parse_args()

    # ── Config ────────────────────────────────────────────────────────────
    from config import ModelConfig, TrainConfig
    model_cfg = ModelConfig()
    train_cfg = TrainConfig()

    if args.device:         train_cfg.device         = args.device
    if args.epochs:         train_cfg.epochs         = args.epochs
    if args.batch_size:     train_cfg.batch_size      = args.batch_size
    if args.lr:             train_cfg.learning_rate  = args.lr
    if args.checkpoint_dir: train_cfg.checkpoint_dir = Path(args.checkpoint_dir)

    # ── Device selection (CUDA → MPS → CPU) ───────────────────────────────
    from device import DeviceConfig, to_device
    dev_cfg = DeviceConfig.auto(train_cfg.device)
    dev_cfg.log_info()
    device = dev_cfg.device

    # ── Data ──────────────────────────────────────────────────────────────
    from dataset import build_dataloaders
    train_loader, val_loader = build_dataloaders(train_cfg, model_cfg)
    logger.info(
        "Dataset: %d train batches, %d val batches",
        len(train_loader),
        len(val_loader),
    )

    # ── Model ─────────────────────────────────────────────────────────────
    import torch
    import torch.nn as nn
    from model import MultimodalPredictor

    model = MultimodalPredictor(model_cfg)
    model = to_device(model, device)

    param_count = sum(p.numel() for p in model.parameters() if p.requires_grad)
    logger.info("Model parameters: %s", f"{param_count:,}")

    # ── Optimiser & loss ──────────────────────────────────────────────────
    optimizer = torch.optim.AdamW(
        model.parameters(),
        lr=train_cfg.learning_rate,
        weight_decay=train_cfg.weight_decay,
    )
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
        optimizer, T_max=train_cfg.epochs
    )
    criterion = nn.CrossEntropyLoss()

    # AMP scaler — only supported on CUDA
    scaler = torch.amp.GradScaler("cuda", enabled=dev_cfg.amp_enabled)

    # ── Checkpoint dir ────────────────────────────────────────────────────
    train_cfg.checkpoint_dir.mkdir(parents=True, exist_ok=True)

    best_val_acc = 0.0

    # ── Training loop ─────────────────────────────────────────────────────
    for epoch in range(1, train_cfg.epochs + 1):
        # — Train —
        model.train()
        total_loss, correct, total = 0.0, 0, 0

        for images, tokens, labels in train_loader:
            images = images.to(device, non_blocking=True)
            tokens = tokens.to(device, non_blocking=True)
            labels = labels.to(device, non_blocking=True)

            optimizer.zero_grad()

            with torch.autocast(device_type=device.type, enabled=dev_cfg.amp_enabled):
                logits = model(images, tokens)
                loss   = criterion(logits, labels)

            scaler.scale(loss).backward()
            scaler.unscale_(optimizer)
            torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
            scaler.step(optimizer)
            scaler.update()

            total_loss += loss.item() * labels.size(0)
            correct    += (logits.argmax(1) == labels).sum().item()
            total      += labels.size(0)

        train_loss = total_loss / total
        train_acc  = correct / total

        # — Validate —
        model.eval()
        val_correct, val_total = 0, 0
        with torch.no_grad():
            for images, tokens, labels in val_loader:
                images = images.to(device, non_blocking=True)
                tokens = tokens.to(device, non_blocking=True)
                labels = labels.to(device, non_blocking=True)

                with torch.autocast(device_type=device.type, enabled=dev_cfg.amp_enabled):
                    logits = model(images, tokens)

                val_correct += (logits.argmax(1) == labels).sum().item()
                val_total   += labels.size(0)

        val_acc = val_correct / val_total
        scheduler.step()

        logger.info(
            "Epoch %3d/%d  loss=%.4f  train_acc=%.3f  val_acc=%.3f  lr=%.2e",
            epoch,
            train_cfg.epochs,
            train_loss,
            train_acc,
            val_acc,
            scheduler.get_last_lr()[0],
        )

        # — Save checkpoint —
        if epoch % train_cfg.save_every == 0 or val_acc > best_val_acc:
            ckpt = {
                "epoch":       epoch,
                "model_state": model.state_dict(),
                "optim_state": optimizer.state_dict(),
                "val_acc":     val_acc,
                "model_cfg":   model_cfg,
            }
            ckpt_path = train_cfg.checkpoint_dir / f"epoch_{epoch:04d}.pt"
            torch.save(ckpt, ckpt_path)

            if val_acc > best_val_acc:
                best_val_acc = val_acc
                best_path = train_cfg.checkpoint_dir / "best.pt"
                torch.save(ckpt, best_path)
                logger.info("  ✓ New best val_acc=%.3f → saved to %s", val_acc, best_path)

        dev_cfg.empty_cache()

    logger.info("Training complete. Best val_acc=%.3f", best_val_acc)


if __name__ == "__main__":
    train()
