# /* spell-checker: disable */

import logging
import logging.config
import os
from datetime import datetime

import fire
import numpy as np
import torch
import torch.nn.functional as F
import torchvision.utils as vutils
from ignite.contrib.handlers import CosineAnnealingScheduler
from ignite.engine import Events
from ignite.metrics import Loss
from ignite.utils import convert_tensor
from sklearn.model_selection import train_test_split
from tensorboardX import SummaryWriter
from toolz import compose
from torch.utils import data
from tqdm import tqdm

from cv_lib.event_handlers import (SnapshotHandler, logging_handlers,
                                   tensorboard_handlers)
from cv_lib.event_handlers.logging_handlers import Evaluator
from cv_lib.event_handlers.tensorboard_handlers import (create_image_writer,
                                                        create_summary_writer)
from cv_lib.segmentation.dutchf3.augmentations import (AddNoise, Compose,
                                                       RandomHorizontallyFlip,
                                                       RandomRotate)
from cv_lib.segmentation.dutchf3.data import (TrainPatchLoader, decode_segmap,
                                              split_train_val, split_non_overlapping_train_val)
from cv_lib.segmentation.dutchf3.engine import (create_supervised_evaluator,
                                                create_supervised_trainer)
from cv_lib.segmentation.dutchf3.metrics import MeanIoU, PixelwiseAccuracy
from cv_lib.segmentation import models
from cv_lib.segmentation.dutchf3.utils import (current_datetime, generate_path,
                                               git_branch, git_hash, np_to_tb)
from default import _C as config
from default import update_config

CLASS_WEIGHTS=[0.7151, 0.8811, 0.5156, 0.9346, 0.9683, 0.9852]

def prepare_batch(batch, device=None, non_blocking=False):
    x, y = batch
    return (
        convert_tensor(x, device=device, non_blocking=non_blocking),
        convert_tensor(y, device=device, non_blocking=non_blocking),
    )

def run(*options, cfg=None):
    """Run training and validation of model

    Notes:
        Options can be passed in via the options argument and loaded from the cfg file
        Options loaded from default.py will be overridden by options loaded from cfg file
        Options passed in through options argument will override option loaded from cfg file
    
    Args:
        *options (str,int ,optional): Options used to overide what is loaded from the config. 
                                      To see what options are available consult default.py
        cfg (str, optional): Location of config file to load. Defaults to None.
    """
    fraction_validation=0.2
    update_config(config, options=options, config_file=cfg)
    logging.config.fileConfig(config.LOG_CONFIG)
    logger = logging.getLogger(__name__)
    logger.debug(config.WORKERS)
    scheduler_step = config.TRAIN.END_EPOCH // config.TRAIN.SNAPSHOTS
    torch.backends.cudnn.benchmark = config.CUDNN.BENCHMARK

    torch.manual_seed(config.SEED)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(config.SEED)
    np.random.seed(seed=config.SEED)

    # Generate the train and validation sets for the model:
    split_train_val(config.DATASET.STRIDE, per_val=fraction_validation, loader_type="patch")

    # Setup Augmentations
    if config.TRAIN.AUGMENTATION:
        data_aug = Compose(
            # [RandomRotate(10), RandomHorizontallyFlip(), AddNoise()]
            [RandomHorizontallyFlip(), AddNoise()]
        )
    else:
        data_aug = None

    train_set = TrainPatchLoader(
        split="train",
        is_transform=True,
        stride=config.DATASET.STRIDE,
        patch_size=config.DATASET.PATCH_SIZE,
        augmentations=data_aug,
    )

    # Without Augmentation:
    val_set = TrainPatchLoader(
        split="val",
        is_transform=True,
        stride=config.DATASET.STRIDE,
        patch_size=config.DATASET.PATCH_SIZE,
    )

    n_classes = train_set.n_classes

    train_loader = data.DataLoader(
        train_set, batch_size=config.TRAIN.BATCH_SIZE_PER_GPU, num_workers=config.WORKERS, shuffle=True
    )
    val_loader = data.DataLoader(
        train_set, batch_size=config.VALIDATION.BATCH_SIZE_PER_GPU, num_workers=config.WORKERS
    )

    model = getattr(models, config.MODEL.NAME).get_seg_model(config)

    device = "cpu"
    if torch.cuda.is_available():
        device = "cuda"
    model = model.to(device)  # Send to GPU

    optimizer = torch.optim.SGD(
        model.parameters(),
        lr=config.TRAIN.MAX_LR,
        momentum=config.TRAIN.MOMENTUM,
        weight_decay=config.TRAIN.WEIGHT_DECAY,
    )

    tboard_log_dir = generate_path(config.LOG_DIR, git_branch(), git_hash(), config.MODEL.NAME, current_datetime())
    summary_writer = create_summary_writer(log_dir=tboard_log_dir)
    snapshot_duration = scheduler_step * len(train_set)
    scheduler = CosineAnnealingScheduler(
        optimizer, "lr", config.TRAIN.MAX_LR, config.TRAIN.MIN_LR, snapshot_duration
    )

    # weights are inversely proportional to the frequency of the classes in the training set
    class_weights = torch.tensor(
        CLASS_WEIGHTS,
        device=device,
        requires_grad=False,
    )
   
    criterion = torch.nn.CrossEntropyLoss(weight=class_weights, ignore_index=255, reduction='mean')

    trainer = create_supervised_trainer(
        model, optimizer, criterion, prepare_batch, device=device
    )

    trainer.add_event_handler(Events.ITERATION_STARTED, scheduler)

    trainer.add_event_handler(
        Events.ITERATION_COMPLETED,
        logging_handlers.log_training_output(log_interval=config.PRINT_FREQ),
    )
    trainer.add_event_handler(Events.EPOCH_STARTED, logging_handlers.log_lr(optimizer))
    trainer.add_event_handler(
        Events.EPOCH_STARTED,
        tensorboard_handlers.log_lr(summary_writer, optimizer, "epoch"),
    )
    trainer.add_event_handler(
        Events.ITERATION_COMPLETED,
        tensorboard_handlers.log_training_output(summary_writer),
    )

    def _select_pred_and_mask(model_out_dict):
        return (model_out_dict["y_pred"].squeeze(), model_out_dict["mask"].squeeze())

    evaluator = create_supervised_evaluator(
        model,
        prepare_batch,
        metrics={
            "IoU": MeanIoU(n_classes, device, output_transform=_select_pred_and_mask),
            "nll": Loss(criterion, output_transform=_select_pred_and_mask),
            "pixa": PixelwiseAccuracy(n_classes, device, output_transform=_select_pred_and_mask)
        },
        device=device,
    )

    # Set the validation run to start on the epoch completion of the training run
    trainer.add_event_handler(Events.EPOCH_COMPLETED, Evaluator(evaluator, val_loader))

    evaluator.add_event_handler(
        Events.EPOCH_COMPLETED,
        logging_handlers.log_metrics(
            "Validation results",
            metrics_dict={"IoU": "IoU :", "nll": "Avg loss :", "pixa": "Pixelwise Accuracy :"},
        ),
    )
    evaluator.add_event_handler(
        Events.EPOCH_COMPLETED,
        tensorboard_handlers.log_metrics(
            summary_writer,
            trainer,
            "epoch",
            metrics_dict={"IoU": "Validation/IoU", "nll": "Validation/Loss"},
        ),
    )

    def _select_max(pred_tensor):
        return pred_tensor.max(1)[1]

    def _tensor_to_numpy(pred_tensor):
        return pred_tensor.squeeze().cpu().numpy()

    transform_func = compose(
        np_to_tb,
        decode_segmap(n_classes=n_classes),
        _tensor_to_numpy
    )

    transform_pred = compose(
        transform_func,
        _select_max
    )

    evaluator.add_event_handler(
        Events.EPOCH_COMPLETED,
        create_image_writer(summary_writer, "Validation/Image", "image"),
    )
    evaluator.add_event_handler(
        Events.EPOCH_COMPLETED,
        create_image_writer(summary_writer, "Validation/Mask", "mask", transform_func=transform_func),
    )
    evaluator.add_event_handler(
        Events.EPOCH_COMPLETED,
        create_image_writer(summary_writer, "Validation/Pred", "y_pred", transform_func=transform_pred),
    )

    def snapshot_function():
        return (trainer.state.iteration % snapshot_duration) == 0

    output_dir = generate_path(config.OUTPUT_DIR, git_branch(), git_hash(), config.MODEL.NAME, current_datetime())
    checkpoint_handler = SnapshotHandler(
        output_dir,
        config.MODEL.NAME,
        snapshot_function,
    )
    evaluator.add_event_handler(
        Events.EPOCH_COMPLETED, checkpoint_handler, {"model": model}
    )
  
    logger.info("Starting training")
    trainer.run(train_loader, max_epochs=config.TRAIN.END_EPOCH)

if __name__ == "__main__":
    fire.Fire(run)
