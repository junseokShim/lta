"""
Inference script for MultimodalPredictor.

Usage
-----
python infer.py                                      # use best.pt, auto device
python infer.py --checkpoint checkpoints/best.pt
python infer.py --device cpu
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run inference with MultimodalPredictor")
    p.add_argument("--checkpoint", default="checkpoints/best.pt", help="Path to checkpoint .pt file")
    p.add_argument("--device",     default="auto",                 help="cuda | mps | cpu | auto")
    p.add_argument("--batch-size", type=int, default=16)
    p.add_argument("--num-samples", type=int, default=64, help="Synthetic samples to predict")
    return p.parse_args()


def infer() -> None:
    args = parse_args()

    ckpt_path = Path(args.checkpoint)
    if not ckpt_path.exists():
        logger.error("Checkpoint not found: %s", ckpt_path)
        logger.error("Run train.py first to generate a checkpoint.")
        raise SystemExit(1)

    # ── Device selection (CUDA → MPS → CPU) ───────────────────────────────
    from device import DeviceConfig, to_device
    dev_cfg = DeviceConfig.auto(args.device)
    dev_cfg.log_info()
    device = dev_cfg.device

    # ── Load checkpoint ───────────────────────────────────────────────────
    import torch
    ckpt = torch.load(ckpt_path, map_location=device)
    model_cfg = ckpt["model_cfg"]
    logger.info("Loaded checkpoint from epoch %d (val_acc=%.3f)", ckpt["epoch"], ckpt["val_acc"])

    # ── Model ─────────────────────────────────────────────────────────────
    from model import MultimodalPredictor
    model = MultimodalPredictor(model_cfg)
    model.load_state_dict(ckpt["model_state"])
    model = to_device(model, device)
    model.eval()

    # ── Synthetic inference data ──────────────────────────────────────────
    from config import TrainConfig
    train_cfg = TrainConfig()

    images = torch.rand(args.num_samples, 3, train_cfg.image_size, train_cfg.image_size)
    tokens = torch.randint(1, model_cfg.vocab_size, (args.num_samples, train_cfg.seq_len))

    # ── Batch inference ───────────────────────────────────────────────────
    all_preds = []
    all_probs  = []

    with torch.no_grad():
        for start in range(0, args.num_samples, args.batch_size):
            img_batch = images[start : start + args.batch_size].to(device)
            tok_batch = tokens[start : start + args.batch_size].to(device)

            with torch.autocast(device_type=device.type, enabled=dev_cfg.amp_enabled):
                logits = model(img_batch, tok_batch)

            probs = torch.softmax(logits.float(), dim=-1)
            preds = probs.argmax(dim=-1)

            all_preds.append(preds.cpu())
            all_probs.append(probs.cpu())

    all_preds = torch.cat(all_preds)
    all_probs = torch.cat(all_probs)

    # ── Summary ───────────────────────────────────────────────────────────
    logger.info("Predicted %d samples on %s", args.num_samples, device)
    for cls_id in range(model_cfg.num_classes):
        count = (all_preds == cls_id).sum().item()
        avg_conf = all_probs[all_preds == cls_id, cls_id].mean().item() if count > 0 else 0.0
        logger.info("  class %2d: %4d samples  avg_confidence=%.3f", cls_id, count, avg_conf)


if __name__ == "__main__":
    infer()
