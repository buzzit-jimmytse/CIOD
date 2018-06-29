# --------------------------------------------------------
# Pytorch multi-GPU Faster R-CNN
# Licensed under The MIT License [see LICENSE for details]
# Written by Jiasen Lu, Jianwei Yang, based on code from Ross Girshick
# --------------------------------------------------------
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import argparse
import os
import pprint

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data.sampler import Sampler
from tqdm import tqdm, trange

import _init_paths
from datasets.samplers.rcnnsampler import RcnnSampler
from model.faster_rcnn.resnet import resnet
from model.faster_rcnn.vgg16 import vgg16
from model.utils.config import cfg, cfg_from_file, cfg_fix
from model.utils.net_utils import adjust_learning_rate, set_learning_rate, save_checkpoint, clip_gradient
from model.utils.net_utils import tensor_holder, ciod_old_and_new
from roi_data_layer.roibatchLoader import roibatchLoader
from roi_data_layer.roidb import combined_roidb


def parse_args():
    """
    Parse input arguments
    """
    parser = argparse.ArgumentParser(description='Train a Fast R-CNN network')

    # Config the session ID for identify
    parser.add_argument('--session', dest='session', default=1, type=int, help='Training session ID')
    parser.add_argument('--group', dest='group', type=int, default=-1, help='Train certain group, or all (-1) groups')

    # Config the session
    parser.add_argument('--dataset', dest='dataset', default='2007', type=str, help='Training dataset, in VOC format')

    # Config the net
    parser.add_argument('--net', dest='net', default='res101', type=str, help='vgg16, res101')
    parser.add_argument('--ls', dest='large_scale', action='store_true', help='Whether use large image scale')
    parser.add_argument('--cag', dest='class_agnostic', action='store_true',
                        help='Whether perform class_agnostic bbox regression')

    # Logging, displaying and saving
    parser.add_argument('--use_tfboard', dest='use_tfboard', action="store_true",
                        help='Whether use tensorflow tensorboard')
    parser.add_argument('--save_dir', dest='save_dir', nargs=argparse.REMAINDER, default="results",
                        help='Directory to save models')
    parser.add_argument('--save_without_repr', dest='save_without_repr', action="store_true",
                        help='Save the model before representation learning')
    # Other config to override
    parser.add_argument('--conf', dest='config_file', type=str, help='Other config(s) to override')

    return parser.parse_args()


if __name__ == '__main__':
    print(_init_paths.lib_path)
    args = parse_args()

    print('Called with args:')
    print(args)

    if args.use_tfboard:
        from tensorboardX import SummaryWriter

        logger = SummaryWriter(os.path.join('logs', '{}_{}'.format(args.session, args.dataset)))

    args.imdb_name = "voc_{}_trainval".format(args.dataset)
    args.imdbval_name = "voc_{}_test".format(args.dataset)
    cfg_from_file("cfgs/{}{}.yml".format(args.net, "_ls" if args.large_scale else ""))
    if args.config_file:
        cfg_from_file(args.config_file)

    cfg_fix()

    print('Using config:')
    pprint.pprint(cfg)
    np.random.seed(cfg.RNG_SEED)

    output_dir = os.path.join(args.save_dir, str(args.session), args.net, args.dataset)
    os.makedirs(output_dir, exist_ok=True)

    # initilize the tensor holders here.
    im_data = tensor_holder(torch.FloatTensor(1), cfg.CUDA, True)
    im_info = tensor_holder(torch.FloatTensor(1), cfg.CUDA, True)
    num_boxes = tensor_holder(torch.LongTensor(1), cfg.CUDA, True)
    gt_boxes = tensor_holder(torch.FloatTensor(1), cfg.CUDA, True)

    # The representation classifier
    class_means = torch.zeros(2048, cfg.NUM_CLASSES + 1)
    # The iCaRL-like training procedure
    class_proto = [[] for _ in range(cfg.CIOD.TOTAL_CLS + 1)]

    group_cls, group_cls_arr, group_merged_arr = ciod_old_and_new(
        cfg.NUM_CLASSES, cfg.CIOD.GROUPS, cfg.CIOD.DISTILL_GROUP)

    # Train ALL groups, or just ONE group
    start_group, end_group = (0, cfg.CIOD.GROUPS) if args.group == -1 else (args.group, args.group + 1)

    # Now we enter the group loop
    for group in trange(start_group, end_group, desc="Group", leave=True):
        now_cls_low, now_cls_high = group_cls[group], group_cls[group + 1]
        max_proto = max(1, cfg.CIOD.TOTAL_PROTO // (now_cls_high - 1))

        # Get the net
        if args.net == 'vgg16':
            fasterRCNN = vgg16(cfg.CLASSES, pretrained=True, class_agnostic=args.class_agnostic)
        elif args.net.startswith('res'):
            fasterRCNN = resnet(cfg.CLASSES, int(args.net[3:]),
                                pretrained=True, class_agnostic=args.class_agnostic)
        else:
            raise KeyError("Unknown Network")

        fasterRCNN.create_architecture()

        if cfg.CUDA:  # Send to GPU
            if cfg.MGPU:
                fasterRCNN = nn.DataParallel(fasterRCNN)
            fasterRCNN.cuda()

        # How to optimize
        params = []
        special_params_index = []
        lr = cfg.TRAIN.LEARNING_RATE

        for ith, (key, value) in enumerate(dict(fasterRCNN.named_parameters()).items()):  # since we froze some layers
            if value.requires_grad:
                if 'RCNN_rpn.RPN_cls_score' in key:  # Record the parameter position of RPN_cls_score
                    special_params_index.append(ith)

                if 'bias' in key:
                    params += [{'params': [value], 'lr': lr * (cfg.TRAIN.DOUBLE_BIAS + 1),
                                'weight_decay': cfg.TRAIN.BIAS_DECAY and cfg.TRAIN.WEIGHT_DECAY or 0}]
                else:
                    params += [{'params': [value], 'lr': lr, 'weight_decay': cfg.TRAIN.WEIGHT_DECAY}]

        if cfg.TRAIN.OPTIMIZER == "adam":
            lr = lr * 0.1
            optimizer = torch.optim.Adam(params)
        elif cfg.TRAIN.OPTIMIZER == "sgd":
            optimizer = torch.optim.SGD(params, momentum=cfg.TRAIN.MOMENTUM)
        else:
            raise KeyError("Unknown Optimizer")

        lr = cfg.TRAIN.LEARNING_RATE  # Reverse the Learning Rate
        if cfg.TRAIN.OPTIMIZER == 'adam':
            lr = lr * 0.1
        set_learning_rate(optimizer, lr)
        fasterRCNN.train()

        # Get database, and merge the class proto
        imdb, roidb, ratio_list, ratio_index = combined_roidb(
            args.dataset, "trainvalStep{}a".format(group), classes=cfg.CLASSES[:now_cls_high], ext=cfg.EXT)

        train_size = len(roidb)
        sampler_batch = RcnnSampler(train_size, cfg.TRAIN.BATCH_SIZE)
        dataset = roibatchLoader(roidb, ratio_list, ratio_index, cfg.TRAIN.BATCH_SIZE, now_cls_high, training=True)
        dataloader = torch.utils.data.DataLoader(
            dataset, batch_size=cfg.TRAIN.BATCH_SIZE, sampler=sampler_batch,
            num_workers=min(cfg.TRAIN.BATCH_SIZE * 2, os.cpu_count()))
        tqdm.write('{:d} roidb entries'.format(len(roidb)))

        iters_per_epoch = train_size // cfg.TRAIN.BATCH_SIZE

        tot_step = 0

        # Here is the training loop
        for epoch in trange(cfg.TRAIN.MAX_EPOCH, desc="Epoch", leave=True):
            loss_temp = 0

            if epoch % cfg.TRAIN.LEARNING_RATE_DECAY_STEP == 0 and epoch > 0:
                adjust_learning_rate(optimizer, cfg.TRAIN.LEARNING_RATE_DECAY_GAMMA)
                lr *= cfg.TRAIN.LEARNING_RATE_DECAY_GAMMA

            data_iter = iter(dataloader)
            for _ in trange(iters_per_epoch, desc="Iter", leave=True):
                tot_step += 1
                data = next(data_iter)
                im_data.data.resize_(data[0].size()).copy_(data[0])
                im_info.data.resize_(data[1].size()).copy_(data[1])
                gt_boxes.data.resize_(data[2].size()).copy_(data[2])
                num_boxes.data.resize_(data[3].size()).copy_(data[3])
                im_path = list(data[4])

                fasterRCNN.zero_grad()
                rois, cls_prob, bbox_pred, \
                rpn_label, rpn_feature, rpn_cls_score, \
                rois_label, pooled_feat, cls_score, \
                rpn_loss_cls, rpn_loss_bbox, RCNN_loss_cls, RCNN_loss_bbox \
                    = fasterRCNN(im_data, im_info, gt_boxes, num_boxes)

                RCNN_loss_cls = F.cross_entropy(cls_score[..., :now_cls_high], rois_label)

                loss = rpn_loss_cls.mean() + rpn_loss_bbox.mean() + RCNN_loss_cls.mean() + RCNN_loss_bbox.mean()

                loss_temp += loss.data[0]

                # backward
                optimizer.zero_grad()
                loss.backward()
                if args.net == "vgg16":
                    clip_gradient(fasterRCNN, 10.)
                optimizer.step()

                if tot_step % cfg.TRAIN.DISPLAY == 0:
                    if tot_step > 0:
                        loss_temp /= cfg.TRAIN.DISPLAY

                    loss_rpn_cls = rpn_loss_cls.mean().data[0]
                    loss_rpn_box = rpn_loss_bbox.mean().data[0]
                    loss_rcnn_cls = RCNN_loss_cls.mean().data[0]
                    loss_rcnn_box = RCNN_loss_bbox.mean().data[0]
                    fg_cnt = torch.sum(rois_label.data.ne(0))
                    bg_cnt = rois_label.data.numel() - fg_cnt

                    tqdm.write("[S{} G{}] lr: {:.2}, loss: {:.4}, fg/bg=({}/{})\n"
                               "\t\t\trpn_cls: {:.4}, rpn_box: {:.4}, rcnn_cls: {:.4}, rcnn_box {:.4}".format(
                        args.session, group, lr, loss_temp, fg_cnt, bg_cnt,
                        loss_rpn_cls, loss_rpn_box, loss_rcnn_cls, loss_rcnn_box))

                    if args.use_tfboard:
                        info = {
                            'loss': loss_temp,
                            'loss_rpn_cls': loss_rpn_cls,
                            'loss_rpn_box': loss_rpn_box,
                            'loss_rcnn_cls': loss_rcnn_cls,
                            'loss_rcnn_box': loss_rcnn_box,
                            'learning_rate': lr
                        }
                        for tag, value in info.items():
                            logger.add_scalar("Group{}/{}".format(group, tag), value, tot_step)

                    loss_temp = 0

        # Save the model
        save_name = os.path.join(
            output_dir,
            'faster_rcnn_{}_{}_{}_{}.pth'.format(args.session, args.net, args.dataset, group))
        save_checkpoint({
            'session': args.session,
            'epoch': cfg.TRAIN.MAX_EPOCH,
            'model': (fasterRCNN.module if cfg.MGPU else fasterRCNN).state_dict(),
            'optimizer': optimizer.state_dict(),
            'pooling_mode': cfg.POOLING_MODE,
            'class_agnostic': args.class_agnostic,
            'cls_means': class_means,
            'cls_proto': class_proto
        }, save_name)
        tqdm.write('save model: {}'.format(save_name))
        print("{0} Group {1} Done {0}".format('=' * 10, group), end="\n" * 5)

    print("{0} All Done {0}".format('=' * 10), end="\n" * 5)
