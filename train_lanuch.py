"""
-*- coding:utf-8 -*-
@author  : jiangmingchao@joyy.sg
@datetime: 2021-0628
@describe: Training loop 
"""
import torch.nn as nn
import torch
import numpy as np
import random
import math
import time
import os
from model.Transformers.CMT.cmt import CmtTi, CmtXS, CmtS, CmtB

from model.CNN.resnet import resnet50
from utils.augments import *
from utils.precise_bn import *
from datetime import datetime
from torch.utils.tensorboard import SummaryWriter
from utils.optimizer_step import Optimizer, build_optimizer
from data.ImagenetDataset import ImageDataset
from model.model_factory import ModelFactory
from torch.utils.data.distributed import DistributedSampler
from torch.nn.parallel import DistributedDataParallel as DataParallel
from torch.utils.data import DataLoader
from torch.cuda.amp import autocast as autocast

from timm.data import Mixup
from timm.loss import LabelSmoothingCrossEntropy, SoftTargetCrossEntropy

import torch.multiprocessing as mp
import torch.distributed as dist
import torch.nn.functional as F
import argparse
import warnings

from apex.amp import scaler
warnings.filterwarnings('ignore')

# apex
try:
    from apex import amp
    from apex.parallel import convert_syncbn_model
    from apex.parallel import DistributedDataParallel as DDP
except Exception as e:
    print("amp have not been import !!!")

# actnn
try:
    import actnn
    actnn.set_optimization_level("L3")
except Exception as e:
    print("actnn have no import !!!")


parser = argparse.ArgumentParser()
# ------ddp
parser.add_argument('--ngpu', type=int, default=1)
parser.add_argument('--rank', default=-1, type=int,
                    help='node rank for distributed training')
parser.add_argument('--dist-backend', default='nccl',
                    type=str, help='distributed backend')
parser.add_argument('--local_rank', default=-1, type=int)
parser.add_argument('--distributed', default=1, type=int,
                    help="use distributed method to training!!")
# ----- data
parser.add_argument('--train_file', type=str,
                    default="/data/jiangmingchao/data/dataset/imagenet/train_oss_imagenet_128w.txt")
parser.add_argument('--val_file', type=str,
                    default="/data/jiangmingchao/data/dataset/imagenet/val_oss_imagenet_128w.txt")
parser.add_argument('--num-classes', type=int)
parser.add_argument('--input_size', type=int, default=224)
parser.add_argument('--crop_size', type=int, default=224)
parser.add_argument('--num_classes', type=int, default=1000)

# ----- checkpoints log dir
parser.add_argument('--checkpoints-path', default='checkpoints', type=str)
parser.add_argument('--log-dir', default='logs', type=str)

# ---- model
parser.add_argument('--model_name', default="R50", type=str)
parser.add_argument('--qkv_bias', default=0, type=int,
                    help="qkv embedding bias")
parser.add_argument('--ape', default=0, type=int,
                    help="absoluate position embeeding")
parser.add_argument('--rpe', default=1, type=int,
                    help="relative position embeeding")
parser.add_argument('--pe_nd', default=1, type=int,
                    help="no distance relative position embeeding")

# ----transformers
parser.add_argument('--patch_size', default=32, type=int)
parser.add_argument('--dim', default=512, type=int,
                    help="token embeeding dims")
parser.add_argument('--depth', default=12, type=int,
                    help="transformers encoder layer numbers")
parser.add_argument('--heads', default=8, type=int,
                    help="Mutil self attention heads numbers")
parser.add_argument('--dim_head', default=64, type=int,
                    help="embeeding dims")
parser.add_argument('--mlp_dim', default=2048, type=int,
                    help="fead forward network fc dimension, simple x4 for the head dims")
parser.add_argument('--dropout', default=0.1, type=float,
                    help="used for attention and mlp dropout")
parser.add_argument('--emb_dropout', default=0.1, type=float,
                    help="embeeding dropout used for token embeeding!!!")

# ---- optimizer
parser.add_argument('--optimizer_name', default="sgd", type=str)
parser.add_argument('--tf_optimizer', default=1, type=int)
parser.add_argument('--lr', default=1e-1, type=float)
parser.add_argument('--weight_decay', default=1e-4, type=float)
parser.add_argument('--momentum', default=0.9, type=float)
parser.add_argument('--batch_size', default=64, type=int)
parser.add_argument('--num_workers', default=8, type=int)
parser.add_argument('--cosine', default=0, type=int)

# clip grad
parser.add_argument('--grad_clip', default=0, type=int)
parser.add_argument('--max_grad_norm', default=5.0, type=float)

# * Mixup params
parser.add_argument('--mixup', type=float, default=0.8,
                    help='mixup alpha, mixup enabled if > 0. (default: 0.8)')
parser.add_argument('--cutmix', type=float, default=1.0,
                    help='cutmix alpha, cutmix enabled if > 0. (default: 1.0)')
parser.add_argument('--cutmix-minmax', type=float, nargs='+', default=None,
                    help='cutmix min/max ratio, overrides alpha and enables cutmix if set (default: None)')
parser.add_argument('--mixup-prob', type=float, default=1.0,
                    help='Probability of performing mixup or cutmix when either/both is enabled')
parser.add_argument('--mixup-switch-prob', type=float, default=0.5,
                    help='Probability of switching to cutmix when both mixup and cutmix enabled')
parser.add_argument('--mixup-mode', type=str, default='batch',
                    help='How to apply mixup/cutmix params. Per "batch", "pair", or "elem"')
parser.add_argument('--smoothing', type=float, default=0.1, help='Label smoothing (default: 0.1)')

# ---- actnn 2-bit
parser.add_argument('--actnn', default=0, type=int)

# ---- train
parser.add_argument('--warmup_epochs', default=5, type=int)
parser.add_argument('--max_epochs', default=90, type=int)
parser.add_argument('--FP16', default=0, type=int)
parser.add_argument('--apex', default=0, type=int)
parser.add_argument('--mode', default='O1', type=str)
parser.add_argument('--amp', default=1, type=int)

# random seed


def setup_seed(seed=100):
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    np.random.seed(seed)
    random.seed(seed)
    torch.backends.cudnn.benchmark = True
    torch.backends.cudnn.deterministic = False


def translate_state_dict(state_dict):
    new_state_dict = {}
    for key, value in state_dict.items():
        if 'module' in key:
            new_state_dict[key[7:]] = value
        else:
            new_state_dict[key] = value
    return new_state_dict


def accuracy(output, target, topk=(1,)):
    with torch.no_grad():
        maxk = max(topk)
        batch_size = target.size(0)
        _, pred = output.topk(k=maxk, dim=1, largest=True, sorted=True)
        pred = pred.t()
        correct = pred.eq(target.view(1, -1).expand_as(pred))
        res = []
        crr = []
        for k in topk:
            correct_k = correct[:k].view(-1).float().sum(0, keepdim=True)
            acc = correct_k.mul_(1/batch_size).item()
            res.append(acc)  # unit: percentage (%)
            crr.append(correct_k)
        return res, crr


class Metric_rank:
    def __init__(self, name):
        self.name = name
        self.sum = 0.0
        self.n = 0

    def update(self, val):
        self.sum += val
        self.n += 1

    @property
    def average(self):
        return self.sum / self.n

data_list = [] 

# main func
def main_worker(args):
    total_rank = torch.cuda.device_count()
    print('rank: {} / {}'.format(args.local_rank, total_rank))
    dist.init_process_group(backend=args.dist_backend)
    torch.cuda.set_device(args.local_rank)

    ngpus_per_node = total_rank

    if args.local_rank == 0:
        if not os.path.exists(args.checkpoints_path):
            os.makedirs(args.checkpoints_path)

    # metric
    train_losses_metric = Metric_rank("train_losses")
    train_accuracy_metric = Metric_rank("train_accuracy")
    train_metric = {"losses": train_losses_metric,
                    "accuracy": train_accuracy_metric}

    # model
    # backbone = ModelFactory.getmodel(args.model_name)
    if args.model_name.lower() == "r50":
        model = resnet50(
            num_classes = args.num_classes,
            pretrained = False
        )
    # elif args.model_name == "vit":
    #     model = backbone(
    #         image_size=args.crop_size,
    #         patch_size=args.patch_size,
    #         num_classes=args.num_classes,
    #         dim=args.dim,
    #         depth=args.depth,
    #         heads=args.heads,
    #         mlp_dim=args.mlp_dim,
    #         dim_head=args.dim_head,
    #         dropout=args.dropout,
    #         emb_dropout=args.emb_dropout
    #     )
    elif args.model_name.lower() == "cmtti":
        model = CmtTi(num_classes=args.num_classes,
                        input_resolution=(args.crop_size, args.crop_size),
                        qkv_bias=True if args.qkv_bias else False,
                        ape=True if args.ape else False,
                        rpe=True if args.rpe else False,
                        pe_nd=True if args.pe_nd else False
                        )
    elif args.model_name.lower() == "cmtxs":
        model = CmtXS(num_classes=args.num_classes,
                      input_resolution=(args.crop_size, args.crop_size),
                      qkv_bias=True if args.qkv_bias else False,
                      ape=True if args.ape else False,
                      rpe=True if args.rpe else False,
                      pe_nd=True if args.pe_nd else False)

    elif args.model_name.lower() == "cmts":
        model = CmtS(num_classes=args.num_classes,
                      input_resolution=(args.crop_size, args.crop_size),
                      qkv_bias=True if args.qkv_bias else False,
                      ape=True if args.ape else False,
                      rpe=True if args.rpe else False,
                      pe_nd=True if args.pe_nd else False)

    elif args.model_name.lower() == "cmtb":
        model = CmtB(num_classes=args.num_classes,
                     input_resolution=(args.crop_size, args.crop_size),
                     qkv_bias=True if args.qkv_bias else False,
                     ape=True if args.ape else False,
                     rpe=True if args.rpe else False,
                     pe_nd=True if args.pe_nd else False)

    else:
        raise NotImplementedError(f"{args.model_name} have not been use!!")

    if args.local_rank == 0:
        print(f"===============model arch ===============")
        print(model)

    # model mode
    model.train()

    if args.actnn:
        model = actnn.QModule(model)
        if args.local_rank == 0:
            print(model)

    if args.apex:
        model = convert_syncbn_model(model)

    # FP16
    if args.FP16:
        model = model.half()
        for bn in get_bn_modules(model):
            bn.float()

    if torch.cuda.is_available():
        model.cuda(args.local_rank)

    # loss
    if args.mixup > 0.:
        # smoothing is handled with mixup label transform
        criterion = SoftTargetCrossEntropy()
    elif args.smoothing:
        criterion = LabelSmoothingCrossEntropy(smoothing=args.smoothing)
    else:
        criterion = nn.CrossEntropyLoss()
    
    # optimizer
    print("optimizer name: ", args.optimizer_name)
    if args.tf_optimizer:
        optimizer = build_optimizer(
            model,
            args.optimizer_name,
            lr=args.lr,
            weights_decay=args.weight_decay
        )
    else:
        optimizer = Optimizer(args.optimizer_name)(
            param=model.parameters(),
            lr=args.lr,
            weight_decay=args.weight_decay
        )
    # print(optimizer)

    if args.apex:
        model, optimizer = amp.initialize(
            model, optimizer, opt_level=args.mode)
        model = DDP(model, delay_allreduce=True)

    else:
        if args.distributed:
            model = DataParallel(model,
                                 device_ids=[args.local_rank],
                                 find_unused_parameters=True)

    # dataset & dataloader
    train_dataset = ImageDataset(
        image_file=args.train_file,
        train_phase=True,
        input_size=args.input_size,
        crop_size=args.crop_size,
        shuffle=True,
        interpolation="bilinear",
        auto_augment="rand",
        color_prob=0.4,
        hflip_prob=0.5
    )

    validation_dataset = ImageDataset(
        image_file=args.val_file,
        train_phase=False,
        input_size=args.input_size,
        crop_size=args.crop_size,
        shuffle=False
    )

    if args.local_rank == 0:
        print("Trainig dataset length: ", len(train_dataset))
        print("Validation dataset length: ", len(validation_dataset))

    # sampler
    if args.distributed:
        train_sampler = DistributedSampler(train_dataset)
        validation_sampler = DistributedSampler(validation_dataset)
    else:
        train_sampler = None
        validation_sampler = None

    # logs
    log_writer = SummaryWriter(args.log_dir)

    # dataloader
    train_loader = DataLoader(
        dataset=train_dataset,
        batch_size=args.batch_size,
        shuffle=(train_sampler is None),
        num_workers=args.num_workers,
        pin_memory=True,
        sampler=train_sampler,
        drop_last=True
    )

    validation_loader = DataLoader(
        dataset=validation_dataset,
        batch_size=args.batch_size,
        shuffle=(validation_sampler is None),
        num_workers=args.num_workers,
        pin_memory=True,
        sampler=validation_sampler,
        drop_last=True
    )

    # mixup & cutmix
    mixup_fn = None
    mixup_active = args.mixup > 0 or args.cutmix > 0. or args.cutmix_minmax is not None
    if mixup_active:
        mixup_fn = Mixup(
            mixup_alpha=args.mixup, cutmix_alpha=args.cutmix, cutmix_minmax=args.cutmix_minmax,
            prob=args.mixup_prob, switch_prob=args.mixup_switch_prob, mode=args.mixup_mode,
            label_smoothing=args.smoothing, num_classes=args.num_classes)
        print("use the mixup function ")


    start_epoch = 1
    batch_iter = 0
    train_batch = math.ceil(len(train_dataset) /
                            (args.batch_size * ngpus_per_node))
    total_batch = train_batch * args.max_epochs
    no_warmup_total_batch = int(
        args.max_epochs - args.warmup_epochs) * train_batch

    if args.amp:
        scaler = torch.cuda.amp.GradScaler()
    else:
        scaler = None

    best_loss, best_acc = np.inf, 0.0
    # training loop
    for epoch in range(start_epoch, args.max_epochs + 1):
        if args.distributed:
            train_sampler.set_epoch(epoch)
        # train for epoch
        batch_iter, scaler = train(args, scaler, train_loader, mixup_fn, model, criterion, optimizer,
                                   epoch, batch_iter, total_batch, train_batch, log_writer, train_metric)

        # calculate the validation with the batch iter
        if epoch % 2 == 0:
            val_loss, val_acc = val(
                args, validation_loader, model, criterion, epoch, log_writer)
            # recored & write
            if args.local_rank == 0:
                best_loss = val_loss
                state_dict = translate_state_dict(model.state_dict())
                state_dict = {
                    'epoch': epoch,
                    'state_dict': state_dict,
                    'optimizer': optimizer.state_dict(),
                }
                torch.save(
                    state_dict,
                    args.checkpoints_path + '/' 'r50' +
                    f'_losses_{best_loss}' + '.pth'
                )

                best_acc = val_acc
                state_dict = translate_state_dict(model.state_dict())
                state_dict = {
                    'epoch': epoch,
                    'state_dict': state_dict,
                    'optimizer': optimizer.state_dict(),
                }
                torch.save(state_dict,
                           args.checkpoints_path + '/' + 'r50' + f'_accuracy_{best_acc}' + '.pth')
        # model mode
        model.train()


# train function
def train(args,
          scaler,
          train_loader,
          mixup_fn,
          model,
          criterion,
          optimizer,
          epoch,
          batch_iter,
          total_batch,
          train_batch,
          log_writer,
          train_metric,
          ):
    """Traing with the batch iter for get the metric
    """
    model.train()
    # device = model.device
    loader_length = len(train_loader)

    for batch_idx, data in enumerate(train_loader):
        batch_start = time.time()
        if args.cosine:
            # cosine learning rate
            lr = cosine_learning_rate(
                args, epoch, batch_iter, optimizer, train_batch
            )
        else:
            # step learning rate
            lr = step_learning_rate(
                args, epoch, batch_iter, optimizer, train_batch
            )

        # forward
        batch_data, batch_label, data_path = data[0], data[1], data[2]

        if args.FP16:
            batch_data = batch_data.half()

        batch_data = batch_data.cuda()
        batch_label = batch_label.cuda()

        # print(batch_data.shape)
        # if torch.isnan(batch_data).float().sum() >= 1:
        #     print(batch_data)
        #     with open(f"/data/jiangmingchao/data/AICutDataset/transformers/CMT/data/nan_{args.local_rank}.txt", "w") as file:
        #         for i in range(len(data_path)):
        #             file.write(data_path[i] + '\t' + str(batch_label[i]) + '\n')
        #     print("There are some error on the batch_data & nan!!!!")
        #     break
        
        # if args.local_rank == 0:
        #     print(batch_iter)

        # mixup or cutmix
        if mixup_fn is not None:
            batch_data, batch_label = mixup_fn(batch_data, batch_label)

        if args.amp:
            with autocast():
                batch_output = model(batch_data)
                losses = criterion(batch_output, batch_label)
                
        else:
            batch_output = model(batch_data)
            losses = criterion(batch_output, batch_label)
        
        # translate the miuxp one hot to float
        if mixup_fn is not None:
            batch_label = batch_label.argmax(dim=1)

        optimizer.zero_grad()

        if args.apex:
            with amp.scale_loss(losses, optimizer) as scaled_loss:
                scaled_loss.backward()
            optimizer.step()

        elif args.amp:
            scaler.scale(losses).backward()
            if args.grad_clip:
                scaler.unscale_(optimizer)
                torch.nn.utils.clip_grad_norm_(
                    model.parameters(), args.max_grad_norm, norm_type=2.0)
            scaler.step(optimizer)
            scaler.update()

        else:
            losses.backward()
            if args.grad_clip:
                torch.nn.utils.clip_grad_norm_(
                    model.parameters(), args.max_grad_norm, norm_type=2.0)
            optimizer.step()

        # calculate the accuracy
        batch_acc, _ = accuracy(batch_output, batch_label)

        # record the average momentum result
        train_metric["losses"].update(losses.data.item())
        train_metric["accuracy"].update(batch_acc[0])

        batch_time = time.time() - batch_start

        batch_iter += 1

        if args.local_rank == 0:
            print("[Training] Time: {} Epoch: [{}/{}] batch_idx: [{}/{}] batch_iter: [{}/{}] batch_losses: {:.4f} batch_accuracy: {:.4f} LearningRate: {:.6f} BatchTime: {:.4f}".format(
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                epoch,
                args.max_epochs,
                batch_idx,
                train_batch,
                batch_iter,
                total_batch,
                losses.data.item(),
                batch_acc[0],
                lr,
                batch_time
            ))

        if args.local_rank == 0:
            # batch record
            record_log(log_writer, losses,
                       batch_acc[0], lr, batch_iter, batch_time)

    if args.local_rank == 0:
        # epoch record
        record_scalars(log_writer, train_metric["losses"].average,
                       train_metric["accuracy"].average, epoch, flag="train")

    return batch_iter, scaler


def val(
        args,
        val_loader,
        model,
        criterion,
        epoch,
        log_writer,
):
    """Validation and get the metric
    """
    model.eval()
    # device = model.device
    criterion = nn.CrossEntropyLoss()
    epoch_losses, epoch_accuracy = 0.0, 0.0

    batch_acc_list = []
    batch_loss_list = []

    with torch.no_grad():
        for batch_idx, data in enumerate(val_loader):
            batch_data, batch_label, _ = data[0], data[1], data[2]

            if args.FP16:
                batch_data = batch_data.half()

            batch_data = batch_data.cuda()
            batch_label = batch_label.cuda()

            if args.amp:
                with autocast():
                    batch_output = model(batch_data)
                    batch_losses = criterion(batch_output, batch_label)
            else:
                batch_output = model(batch_data)
                batch_losses = criterion(batch_output, batch_label)

            batch_accuracy, _ = accuracy(batch_output, batch_label)

            batch_acc_list.append(batch_accuracy[0])
            batch_loss_list.append(batch_losses.data.item())

    epoch_acc = np.mean(batch_acc_list)
    epoch_loss = np.mean(batch_loss_list)

    # all reduce the correct number
    # dist.all_reduce(epoch_accuracy, op=dist.ReduceOp.SUM)

    if args.local_rank == 0:
        print(
            f"Validation Epoch: [{epoch}/{args.max_epochs}] Epoch_mean_losses: {epoch_loss} Epoch_mean_accuracy: {epoch_acc}")

        record_scalars(log_writer, epoch_loss, epoch_acc, epoch, flag="val")

    return epoch_loss, epoch_acc


def record_scalars(log_writer, mean_loss, mean_acc, epoch, flag="train"):
    log_writer.add_scalar(f"{flag}/epoch_average_loss", mean_loss, epoch)
    log_writer.add_scalar(f"{flag}/epoch_average_acc", mean_acc, epoch)


# batch scalar record
def record_log(log_writer, losses, acc, lr, batch_iter, batch_time, flag="Train"):
    log_writer.add_scalar(f"{flag}/batch_loss", losses.data.item(), batch_iter)
    log_writer.add_scalar(f"{flag}/batch_acc", acc, batch_iter)
    log_writer.add_scalar(f"{flag}/learning_rate", lr, batch_iter)
    log_writer.add_scalar(f"{flag}/batch_time", batch_time, batch_iter)


def step_learning_rate(args, epoch, batch_iter, optimizer, train_batch):
    """Sets the learning rate
    # Adapted from PyTorch Imagenet example:
    # https://github.com/pytorch/examples/blob/master/imagenet/main.py
    """
    total_epochs = args.max_epochs
    warm_epochs = args.warmup_epochs
    if epoch <= warm_epochs:
        lr_adj = (batch_iter + 1) / (warm_epochs * train_batch)
    elif epoch < int(0.3 * total_epochs):
        lr_adj = 1.
    elif epoch < int(0.6 * total_epochs):
        lr_adj = 1e-1
    elif epoch < int(0.8 * total_epochs):
        lr_adj = 1e-2
    else:
        lr_adj = 1e-3

    for param_group in optimizer.param_groups:
        param_group['lr'] = args.lr * lr_adj
    return args.lr * lr_adj


def cosine_learning_rate(args, epoch, batch_iter, optimizer, train_batch):
    """Cosine Learning rate 
    """
    total_epochs = args.max_epochs
    warm_epochs = args.warmup_epochs
    if epoch <= warm_epochs:
        lr_adj = (batch_iter + 1) / (warm_epochs * train_batch)
    else:
        lr_adj = 1/2 * (1 + math.cos(batch_iter * math.pi /
                                     ((total_epochs - warm_epochs) * train_batch)))

    for param_group in optimizer.param_groups:
        param_group['lr'] = args.lr * lr_adj
    return args.lr * lr_adj


if __name__ == "__main__":
    args = parser.parse_args()
    setup_seed()

    main_worker(args)
