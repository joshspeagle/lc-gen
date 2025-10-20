"""
Train Transformer autoencoder on light curve time series.

Supports training with block masking and time-aware positional encoding.
"""

import numpy as np
import argparse
import sys
from pathlib import Path
from functools import partial
from tqdm import tqdm
import h5py
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import Dataset, DataLoader, random_split
import matplotlib.pyplot as plt

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from lcgen.models.transformer import TimeSeriesTransformer, TransformerConfig
from lcgen.data.masking import dynamic_block_mask


class LightCurveDataset(Dataset):
    """Dataset for light curve time series with timestamps."""

    def __init__(self, flux, timestamps):
        """
        Args:
            flux: Numpy array of shape (N, seq_len)
            timestamps: Numpy array of shape (N, seq_len)
        """
        self.flux = torch.from_numpy(flux).float()
        self.timestamps = torch.from_numpy(timestamps).float()

    def __len__(self):
        return len(self.flux)

    def __getitem__(self, idx):
        # Return flux and timestamps - masking will be applied in collate_fn
        return {
            'flux': self.flux[idx],
            'time': self.timestamps[idx]
        }


def collate_with_masking(batch, min_block_size=1, max_block_size=None,
                         min_mask_ratio=0.1, max_mask_ratio=0.9):
    """
    Custom collate function that applies masking to light curves.

    Creates input as [masked_flux, mask_indicator] for the transformer.
    """
    # Extract flux and timestamps
    batch_flux = torch.stack([item['flux'] for item in batch], dim=0)  # [batch_size, seq_len]
    batch_time = torch.stack([item['time'] for item in batch], dim=0)  # [batch_size, seq_len]

    batch_size, seq_len = batch_flux.shape

    if max_block_size is None:
        max_block_size = seq_len // 2

    # Apply masking per sample
    batch_inputs = []
    batch_targets = []
    batch_masks = []
    batch_block_sizes = []
    batch_mask_ratios = []

    for i in range(batch_size):
        flux = batch_flux[i]

        # Apply dynamic block masking to flux
        flux_masked, mask, block_size, mask_ratio = dynamic_block_mask(
            flux,
            min_block_size=min_block_size,
            max_block_size=max_block_size,
            min_mask_ratio=min_mask_ratio,
            max_mask_ratio=max_mask_ratio
        )

        # Stack flux and mask as two channels: [masked_flux, mask_indicator]
        # Shape: (seq_len, 2)
        input_with_mask = torch.stack([flux_masked, mask.float()], dim=-1)

        batch_inputs.append(input_with_mask)
        batch_targets.append(flux)
        batch_masks.append(mask)
        batch_block_sizes.append(block_size)
        batch_mask_ratios.append(mask_ratio)

    return {
        'input': torch.stack(batch_inputs),  # (batch, seq_len, 2)
        'target': torch.stack(batch_targets),  # (batch, seq_len)
        'time': batch_time,  # (batch, seq_len)
        'mask': torch.stack(batch_masks),  # (batch, seq_len)
        'block_size': torch.tensor(batch_block_sizes),
        'mask_ratio': torch.tensor(batch_mask_ratios)
    }


def load_lightcurve_data(hdf5_file):
    """
    Load light curve data from HDF5 file.

    Expected structure:
        - 'flux': (N, seq_len) - normalized flux values
        - 'time': (N, seq_len) - timestamps

    Args:
        hdf5_file: Path to HDF5 file

    Returns:
        flux: Numpy array of shape (N, seq_len)
        timestamps: Numpy array of shape (N, seq_len)
    """
    with h5py.File(hdf5_file, 'r') as f:
        flux = f['flux'][:]
        timestamps = f['time'][:]
        print(f"Loaded flux: {flux.shape}, time: {timestamps.shape}")

        # Check for NaNs
        n_nans_flux = np.sum(np.isnan(flux))
        n_nans_time = np.sum(np.isnan(timestamps))
        if n_nans_flux > 0:
            print(f"Warning: {n_nans_flux} NaN values found in flux")
            flux = np.nan_to_num(flux, nan=0.0)
        if n_nans_time > 0:
            print(f"Warning: {n_nans_time} NaN values found in timestamps")
            timestamps = np.nan_to_num(timestamps, nan=0.0)

        return flux, timestamps


def train_epoch(model, dataloader, optimizer, criterion, device, scheduler=None):
    """Train for one epoch."""
    model.train()
    total_loss = 0
    total_masked_loss = 0
    total_unmasked_loss = 0
    n_batches = 0

    for batch in dataloader:
        x_input = batch['input'].to(device)  # (batch, seq_len, 2) [flux, mask]
        x_target = batch['target'].to(device)  # (batch, seq_len)
        timestamps = batch['time'].to(device)  # (batch, seq_len)
        mask = batch['mask'].to(device)  # (batch, seq_len)

        # Forward pass
        optimizer.zero_grad()
        output = model(x_input, timestamps)
        x_recon = output['reconstructed'].squeeze(-1)  # (batch, seq_len)

        # Compute loss over ALL regions (both masked and unmasked)
        loss = criterion(x_recon, x_target)

        # Also track masked vs unmasked separately for monitoring
        masked_loss = criterion(x_recon[mask], x_target[mask])
        unmasked_loss = criterion(x_recon[~mask], x_target[~mask])

        # Backprop and optimize on full reconstruction loss
        loss.backward()

        # Gradient clipping to prevent exploding gradients
        torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)

        optimizer.step()

        # Step scheduler after each batch (for OneCycleLR)
        if scheduler is not None:
            scheduler.step()

        total_loss += loss.item()
        total_masked_loss += masked_loss.item()
        total_unmasked_loss += unmasked_loss.item()
        n_batches += 1

    return {
        'total_loss': total_loss / n_batches,
        'masked_loss': total_masked_loss / n_batches,
        'unmasked_loss': total_unmasked_loss / n_batches
    }


def validate_epoch(model, dataloader, criterion, device):
    """Validate for one epoch."""
    model.eval()
    total_loss = 0
    total_masked_loss = 0
    total_unmasked_loss = 0
    n_batches = 0

    with torch.no_grad():
        for batch in dataloader:
            x_input = batch['input'].to(device)
            x_target = batch['target'].to(device)
            timestamps = batch['time'].to(device)
            mask = batch['mask'].to(device)

            # Forward pass
            output = model(x_input, timestamps)
            x_recon = output['reconstructed'].squeeze(-1)

            # Compute loss
            loss = criterion(x_recon, x_target)
            masked_loss = criterion(x_recon[mask], x_target[mask])
            unmasked_loss = criterion(x_recon[~mask], x_target[~mask])

            total_loss += loss.item()
            total_masked_loss += masked_loss.item()
            total_unmasked_loss += unmasked_loss.item()
            n_batches += 1

    return {
        'total_loss': total_loss / n_batches,
        'masked_loss': total_masked_loss / n_batches,
        'unmasked_loss': total_unmasked_loss / n_batches
    }


def plot_reconstruction_examples(model, dataloader, device, save_path, n_examples=4):
    """Plot reconstruction examples."""
    model.eval()

    # Get one batch
    batch = next(iter(dataloader))
    x_input = batch['input'].to(device)[:n_examples]
    x_target = batch['target'].to(device)[:n_examples]
    timestamps = batch['time'].to(device)[:n_examples]
    mask = batch['mask'].to(device)[:n_examples]
    block_sizes = batch['block_size'][:n_examples].numpy()
    mask_ratios = batch['mask_ratio'][:n_examples].numpy()

    with torch.no_grad():
        output = model(x_input, timestamps)
        x_recon = output['reconstructed'].squeeze(-1)

    # Move to CPU for plotting
    x_target = x_target.cpu().numpy()
    x_recon = x_recon.cpu().numpy()
    timestamps_np = timestamps.cpu().numpy()
    mask = mask.cpu().numpy()

    # Create plots
    fig, axes = plt.subplots(n_examples, 1, figsize=(12, 3*n_examples))
    if n_examples == 1:
        axes = [axes]

    for i, ax in enumerate(axes):
        t = timestamps_np[i]
        target = x_target[i]
        recon = x_recon[i]
        m = mask[i]
        block_size = block_sizes[i]
        mask_ratio = mask_ratios[i]

        # Plot target and reconstruction
        ax.plot(t, target, 'k-', alpha=0.6, label='Target', linewidth=1.5)
        ax.plot(t, recon, 'b-', alpha=0.8, label='Reconstruction', linewidth=1.5)

        # Highlight masked regions
        if m.sum() > 0:
            # Find contiguous masked regions
            masked_indices = np.where(m)[0]
            ax.scatter(t[masked_indices], recon[masked_indices],
                      c='red', s=10, alpha=0.5, label='Masked regions')

        ax.legend()
        ax.set_xlabel('Time')
        ax.set_ylabel('Flux')
        ax.set_title(f'Example {i+1} (masked {mask_ratio*100:.1f}%, block size {block_size})')
        ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(save_path, dpi=150)
    plt.close()
    print(f"Saved reconstruction plot to {save_path}")


def main():
    parser = argparse.ArgumentParser(description='Train Transformer autoencoder on light curves')
    parser.add_argument('--input', type=str,
                        default='data/mock_lightcurves/timeseries.h5',
                        help='Path to HDF5 file with light curve time series')
    parser.add_argument('--output_dir', type=str, default='models/transformer',
                        help='Directory to save model checkpoints')
    parser.add_argument('--epochs', type=int, default=50,
                        help='Number of training epochs')
    parser.add_argument('--batch_size', type=int, default=32,
                        help='Batch size')
    parser.add_argument('--lr', type=float, default=1e-4,
                        help='Learning rate')
    parser.add_argument('--encoder_dims', type=int, nargs='+', default=[64, 128, 256, 512],
                        help='Encoder dimensions for hierarchical levels')
    parser.add_argument('--nhead', type=int, default=4,
                        help='Number of attention heads')
    parser.add_argument('--num_layers', type=int, default=4,
                        help='Number of hierarchical levels')
    parser.add_argument('--num_transformer_blocks', type=int, default=2,
                        help='Transformer blocks per level')
    parser.add_argument('--mask_ratio', type=float, default=0.5,
                        help='Maximum masking ratio')
    parser.add_argument('--block_size', type=int, default=32,
                        help='Maximum block size for masking')
    parser.add_argument('--val_split', type=float, default=0.15,
                        help='Validation set fraction')
    parser.add_argument('--seed', type=int, default=42,
                        help='Random seed')
    parser.add_argument('--device', type=str, default='auto',
                        help='Device (cuda/cpu/auto)')
    parser.add_argument('--save_every', type=int, default=10,
                        help='Save checkpoint every N epochs')

    args = parser.parse_args()

    # Set random seed
    torch.manual_seed(args.seed)
    np.random.seed(args.seed)

    # Device
    if args.device == 'auto':
        device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    else:
        device = torch.device(args.device)
    print(f"Using device: {device}")

    # Create output directory
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Load data
    print(f"\nLoading data from {args.input}...")
    flux, timestamps = load_lightcurve_data(args.input)

    print(f"\nDataset statistics:")
    print(f"  Shape: {flux.shape}")
    print(f"  Flux range: [{np.min(flux):.3f}, {np.max(flux):.3f}]")
    print(f"  Flux mean: {np.mean(flux):.3f}")
    print(f"  Flux std: {np.std(flux):.3f}")
    print(f"  Time range: [{np.min(timestamps):.3f}, {np.max(timestamps):.3f}]")

    # Create dataset
    dataset = LightCurveDataset(flux, timestamps)

    # Split into train/val
    val_size = int(len(dataset) * args.val_split)
    train_size = len(dataset) - val_size
    train_dataset, val_dataset = random_split(
        dataset,
        [train_size, val_size],
        generator=torch.Generator().manual_seed(args.seed)
    )

    print(f"\nDataset split:")
    print(f"  Train: {train_size} samples")
    print(f"  Val: {val_size} samples")

    # Create collate function with masking parameters
    collate_fn = partial(
        collate_with_masking,
        min_block_size=1,
        max_block_size=args.block_size,
        min_mask_ratio=0.1,
        max_mask_ratio=args.mask_ratio
    )

    # Create data loaders
    train_loader = DataLoader(
        train_dataset,
        batch_size=args.batch_size,
        shuffle=True,
        num_workers=0,
        collate_fn=collate_fn,
        pin_memory=True if device.type == 'cuda' else False
    )

    val_loader = DataLoader(
        val_dataset,
        batch_size=args.batch_size,
        shuffle=False,
        num_workers=0,
        collate_fn=collate_fn,
        pin_memory=True if device.type == 'cuda' else False
    )

    # Create model
    seq_len = flux.shape[1]
    print(f"\nCreating Hierarchical Transformer autoencoder...")
    config = TransformerConfig(
        input_dim=2,  # [flux, mask]
        input_length=seq_len,
        encoder_dims=args.encoder_dims,
        nhead=args.nhead,
        num_layers=args.num_layers,
        num_transformer_blocks=args.num_transformer_blocks,
        dropout=0.0,  # No dropout
        min_period=0.00278,
        max_period=1640.0
    )

    model = TimeSeriesTransformer(config).to(device)

    # Count parameters
    n_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print(f"Model parameters: {n_params:,}")

    # Optimizer and scheduler
    optimizer = optim.AdamW(model.parameters(), lr=args.lr, weight_decay=0.01)

    # OneCycleLR scheduler
    total_steps = len(train_loader) * args.epochs
    scheduler = optim.lr_scheduler.OneCycleLR(
        optimizer,
        max_lr=args.lr,
        total_steps=total_steps,
        pct_start=0.1,
        div_factor=1e2,
        final_div_factor=1e2
    )

    # Loss function
    criterion = nn.MSELoss()

    # Training loop
    print(f"\nStarting training for {args.epochs} epochs...")
    max_block_display = args.block_size if args.block_size else "seq_len//2"
    print(f"Dynamic masking: block size [1, {max_block_display}], "
          f"mask ratio [10%, {args.mask_ratio*100:.0f}%]")

    best_val_loss = float('inf')
    train_losses = []
    val_losses = []

    for epoch in range(args.epochs):
        # Train
        train_metrics = train_epoch(model, train_loader, optimizer, criterion, device, scheduler)
        train_losses.append(train_metrics['total_loss'])

        # Validate
        val_metrics = validate_epoch(model, val_loader, criterion, device)
        val_losses.append(val_metrics['total_loss'])

        # Print progress (match MLP format)
        print(f"Epoch {epoch+1}/{args.epochs}")
        print(f"  Train - Loss: {train_metrics['total_loss']:.6f} | "
              f"Masked: {train_metrics['masked_loss']:.6f} | "
              f"Unmasked: {train_metrics['unmasked_loss']:.6f}")
        print(f"  Val   - Loss: {val_metrics['total_loss']:.6f} | "
              f"Masked: {val_metrics['masked_loss']:.6f} | "
              f"Unmasked: {val_metrics['unmasked_loss']:.6f}")

        # Save best model
        if val_metrics['total_loss'] < best_val_loss:
            best_val_loss = val_metrics['total_loss']
            checkpoint_path = output_dir / 'transformer_best.pt'
            torch.save({
                'epoch': epoch,
                'model_state_dict': model.state_dict(),
                'optimizer_state_dict': optimizer.state_dict(),
                'train_loss': train_metrics['total_loss'],
                'val_loss': best_val_loss,
                'config': config
            }, checkpoint_path)
            print(f"  Saved best model to {checkpoint_path}")

        # Plot samples every 10 epochs
        if (epoch + 1) % 10 == 0 or epoch == 0:
            plot_path = output_dir / f'transformer_recon_epoch{epoch+1}.png'
            plot_reconstruction_examples(
                model, val_loader, device, plot_path
            )

        # Save periodic checkpoints
        if (epoch + 1) % args.save_every == 0:
            torch.save({
                'epoch': epoch,
                'model_state_dict': model.state_dict(),
                'optimizer_state_dict': optimizer.state_dict(),
                'val_loss': val_metrics['total_loss'],
                'config': config
            }, output_dir / f'transformer_checkpoint_epoch{epoch+1}.pt')

    # Save final model
    final_path = output_dir / 'transformer_final.pt'
    torch.save({
        'epoch': args.epochs,
        'model_state_dict': model.state_dict(),
        'optimizer_state_dict': optimizer.state_dict(),
        'config': config
    }, final_path)
    print(f"\nSaved final model to {final_path}")

    # Plot training curves
    plt.figure(figsize=(10, 6))
    plt.plot(train_losses, label='Train Loss')
    plt.plot(val_losses, label='Val Loss')
    plt.xlabel('Epoch')
    plt.ylabel('MSE Loss')
    plt.title('Hierarchical Transformer Training')
    plt.legend()
    plt.grid(True, alpha=0.3)
    plt.savefig(output_dir / 'transformer_training_curve.png', dpi=150)
    plt.close()

    print(f"\nTraining complete!")
    print(f"Best validation loss: {best_val_loss:.6f}")


if __name__ == '__main__':
    main()
