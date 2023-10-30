# ------------------------------------------------------------------------
# DETR
# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
# ------------------------------------------------------------------------
# Additionally modified by NAVER Corp. for ViDT
# ------------------------------------------------------------------------
"""
Train and eval functions used in main.py
"""
import math
import sys
from typing import Iterable
import torch

import util.misc as utils
from datasets.coco_eval import CocoEvaluator
from datasets import  get_coco_api_from_dataset



def train_one_epoch(model: torch.nn.Module, criterion: torch.nn.Module,
                    data_loader: Iterable, optimizer: torch.optim.Optimizer,
                    device: torch.device, epoch: int,lr_scheduler,  max_norm: float = 0,
                    n_iter_to_acc: int = 1, print_freq: int = 100):
    """
    Training one epoch

    Parameters:
        model: a target model
        criterion: a critetrion module to compute training (or val, test) loss
        data_loader: a training data laoder to use
        optimizer: an optimizer to use
        epoch: the current epoch number
        max_norm: a max norm for gradient clipping (default=0)
        n_iter_to_acc: the step size for gradient accumulation (default=1)
        print_freq: the step size to print training logs (default=100)

    Return:
        dict: a log dictionary with keys (log type) and values (log value)
    """

    model.train()
    criterion.train()

    # register log types
    metric_logger = utils.MetricLogger(delimiter=", ")
    metric_logger.add_meter('lr', utils.SmoothedValue(window_size=1, fmt='{value:.6f}'))
    metric_logger.add_meter('class_error', utils.SmoothedValue(window_size=1, fmt='{value:.2f}'))
    header = 'Epoch: [{}]'.format(epoch)
    # print_freq = print_freq
    print_freq = 100

    batch_idx = 0
    # iterate one epoch
    for samples, targets, sketches in metric_logger.log_every(data_loader, print_freq, header):

        samples = samples.to(device)
        targets = [{k: v.to(device) for k, v in t.items()} for t in targets]
        sketches = torch.cat([sketch.unsqueeze(0) for sketch in sketches], dim=0)
        sketches = sketches.to(device)
        
        outputs = model([samples.tensors, samples.mask], sketches)

        # compute loss
        loss_dict = criterion(outputs, targets)
        weight_dict = criterion.weight_dict
        
        
        losses = sum(loss_dict[k] * weight_dict[k] for k in loss_dict.keys() if k in weight_dict)

        # reduce losses over all GPUs for logging purposes
        loss_dict_reduced = utils.reduce_dict(loss_dict)

        loss_dict_reduced_unscaled = {f'{k}_unscaled': v
                                      for k, v in loss_dict_reduced.items()}
        loss_dict_reduced_scaled = {k: v * weight_dict[k]
                                    for k, v in loss_dict_reduced.items() if k in weight_dict}
        losses_reduced_scaled = sum(loss_dict_reduced_scaled.values())

        loss_value = losses_reduced_scaled.item()

        if not math.isfinite(loss_value):
            print("Loss is {}, stopping training".format(loss_value))
            print(loss_dict_reduced)
            sys.exit(1)

        # backprop.
        losses /= float(n_iter_to_acc)
        losses.backward()
        if (batch_idx + 1) % n_iter_to_acc == 0:
            if max_norm > 0:
                torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm)

            optimizer.step()
            optimizer.zero_grad()
        # lr_scheduler.step()
        
        # save logs per iteration
        metric_logger.update(loss=loss_value, **loss_dict_reduced_scaled, **loss_dict_reduced_unscaled)
        metric_logger.update(class_error=loss_dict_reduced['class_error'])
        metric_logger.update(lr=optimizer.param_groups[0]["lr"])
        

    # gather the stats from all processes
    metric_logger.synchronize_between_processes()
    print("Averaged stats:", metric_logger)
    return {k: meter.global_avg for k, meter in metric_logger.meters.items()}


def train_one_epoch_with_teacher(model: torch.nn.Module, teacher_model: torch.nn.Module, criterion: torch.nn.Module,
                    data_loader: Iterable, optimizer: torch.optim.Optimizer,
                    device: torch.device, epoch: int, max_norm: float = 0,
                    n_iter_to_acc: int = 1, print_freq: int = 100):
    """
    Training one epoch

    Parameters:
        model: a target model
        teacher_model: a teacher model for distillation
        criterion: a critetrion module to compute training (or val, test) loss
        data_loader: a training data laoder to use
        optimizer: an optimizer to use
        epoch: the current epoch number
        max_norm: a max norm for gradient clipping (default=0)
        n_iter_to_acc: the step size for gradient accumulation (default=1)
        print_freq: the step size to print training logs (default=100)

    Return:
        dict: a log dictionary with keys (log type) and values (log value)
    """

    model.train()
    teacher_model.eval()
    criterion.train()

    # register log types
    metric_logger = utils.MetricLogger(delimiter=", ")
    metric_logger.add_meter('lr', utils.SmoothedValue(window_size=1, fmt='{value:.6f}'))
    metric_logger.add_meter('class_error', utils.SmoothedValue(window_size=1, fmt='{value:.2f}'))
    header = 'Epoch: [{}]'.format(epoch)
    print_freq = print_freq

    batch_idx = 0
    # iterate one epoch
    for samples, targets in metric_logger.log_every(data_loader, print_freq, header):

        samples = samples.to(device)
        targets = [{k: v.to(device) for k, v in t.items()} for t in targets]

        # inference
        outputs = model(samples)
        teacher_outputs = teacher_model(samples)

        # collect distillation token for matching loss
        distil_tokens = (outputs['distil_tokens'], teacher_outputs['distil_tokens'])

        # compute loss
        loss_dict = criterion(outputs, targets, distil_tokens=distil_tokens)
        weight_dict = criterion.weight_dict
        losses = sum(loss_dict[k] * weight_dict[k] for k in loss_dict.keys() if k in weight_dict)

        # reduce losses over all GPUs for logging purposes
        loss_dict_reduced = utils.reduce_dict(loss_dict)

        loss_dict_reduced_unscaled = {f'{k}_unscaled': v
                                      for k, v in loss_dict_reduced.items()}
        loss_dict_reduced_scaled = {k: v * weight_dict[k]
                                    for k, v in loss_dict_reduced.items() if k in weight_dict}
        losses_reduced_scaled = sum(loss_dict_reduced_scaled.values())

        loss_value = losses_reduced_scaled.item()


        if not math.isfinite(loss_value):
            print("Loss is {}, stopping training".format(loss_value))
            print(loss_dict_reduced)
            sys.exit(1)

        # backprop.
        losses.backward()
        if (batch_idx + 1) % n_iter_to_acc == 0:
            if max_norm > 0:
                torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm)

            optimizer.step()
            optimizer.zero_grad()

        # save logs per iteration
        metric_logger.update(loss=loss_value, **loss_dict_reduced_scaled, **loss_dict_reduced_unscaled)
        metric_logger.update(class_error=loss_dict_reduced['class_error'])
        metric_logger.update(lr=optimizer.param_groups[0]["lr"])
        batch_idx += 1

    # gather the stats from all processes
    metric_logger.synchronize_between_processes()
    print("Averaged stats:", metric_logger)
    return {k: meter.global_avg for k, meter in metric_logger.meters.items()}

def inverse_normalize(tensor, mean, std):
    for t, m, s in zip(tensor, mean, std):
        t.mul_(s).add_(m)
    return tensor


@torch.no_grad()
def evaluate(model, criterion, postprocessors, data_loader, base_ds, device, plot=False):
    """
    Training one epoch

    Parameters:
        model: a target model
        criterion: a critetrion module to compute training (or val, test) loss
        postprocessors: a postprocessor to compute AP
        data_loader: an eval data laoder to use
        base_ds: a base dataset class
        device: the device to use (GPU or CPU)

    Return:
        dict: a log dictionary with keys (log type) and values (log value)
    """

    model.eval()
    criterion.eval()

    # register log types
    metric_logger = utils.MetricLogger(delimiter=", ")
    metric_logger.add_meter('class_error', utils.SmoothedValue(window_size=1, fmt='{value:.2f}'))
    header = 'Test:'

    # return eval. metrics
    iou_types = tuple(k for k in ('segm', 'bbox') if k in postprocessors.keys())
    base_ds = get_coco_api_from_dataset(data_loader.dataset)
  

    coco_evaluator = CocoEvaluator(base_ds, iou_types)

    # iterate for all eval. examples
    count = 0
    for samples, targets, sketches in metric_logger.log_every(data_loader, 256, header):
        samples = samples.to(device)
        targets = [{k: v.to(device) for k, v in t.items()} for t in targets]

        sketches = torch.cat([sketch.unsqueeze(0) for sketch in sketches], dim=0)

        sketches = sketches.to(device)
        # inference

        outputs = model([samples.tensors, samples.mask], sketches)
        
        count += 1
       
        # loss compute
        new_targets = targets
        for i in range(len(targets)):
            new_targets[i]['boxes'] = targets[i]['new_boxes']
        loss_dict = criterion(outputs, new_targets)
        weight_dict = criterion.weight_dict
        # reduce losses over all GPUs for logging purposes
        loss_dict_reduced = utils.reduce_dict(loss_dict)
        loss_dict_reduced_scaled = {k: v * weight_dict[k]
                                    for k, v in loss_dict_reduced.items() if k in weight_dict}
        loss_dict_reduced_unscaled = {f'{k}_unscaled': v
                                    for k, v in loss_dict_reduced.items()}
        metric_logger.update(loss=sum(loss_dict_reduced_scaled.values()),
                            **loss_dict_reduced_scaled,
                            **loss_dict_reduced_unscaled)
        metric_logger.update(class_error=loss_dict_reduced['class_error'])

        # compute AP, etc
        orig_target_sizes = torch.stack([t["orig_size"] for t in targets], dim=0)

        results = postprocessors['bbox'](outputs, orig_target_sizes, cosine=True)
            
        if 'segm' in postprocessors.keys():
            target_sizes = torch.stack([t["size"] for t in targets], dim=0)
            results = postprocessors['segm'](results, outputs, orig_target_sizes, target_sizes)
        res = {target['image_id'].item(): output for target, output in zip(targets, results)}
        
        if coco_evaluator is not None:
            coco_evaluator.update(res)

    # gather the stats from all processes
    metric_logger.synchronize_between_processes()
    print("Averaged stats:", metric_logger)
    if coco_evaluator is not None:
        coco_evaluator.synchronize_between_processes()

    # accumulate predictions from all images
    if coco_evaluator is not None:
        coco_evaluator.accumulate()
        coco_evaluator.summarize()

    panoptic_res = None
    stats = {k: meter.global_avg for k, meter in metric_logger.meters.items()}
    if coco_evaluator is not None:
        if 'bbox' in postprocessors.keys():
            stats['coco_eval_bbox'] = coco_evaluator.coco_eval['bbox'].stats.tolist()
        if 'segm' in postprocessors.keys():
            stats['coco_eval_masks'] = coco_evaluator.coco_eval['segm'].stats.tolist()

    if panoptic_res is not None:
        stats['PQ_all'] = panoptic_res["All"]
        stats['PQ_th'] = panoptic_res["Things"]
        stats['PQ_st'] = panoptic_res["Stuff"]

    stats_v2 = {}
    for key, value in stats.items():
        if not key == 'coco_eval_bbox':
            stats_v2['val_'+key] = value 

    return stats, coco_evaluator
