from __future__ import division
import os.path
import numpy as np
from unet import INPUT_SIZE, OUTPUT_SIZE
import normalize
import gzip
import cPickle as pickle
import loss_weighting
import skimage.morphology
from augment import augment
import time
import re

from params import params as P

_EPSILON = 1e-8

def get_image(filename, deterministic):
    with gzip.open(filename,'rb') as f:
        lung = pickle.load(f)

    truth_filename = filename.replace('lung','nodule')
    segmentation_filename = filename.replace('lung','lung_masks')
    segmentation_filename = re.sub(r'subset[0-9]','',segmentation_filename)

    if os.path.isfile(truth_filename):
        with gzip.open(truth_filename,'rb') as f:
            truth = np.array(pickle.load(f),dtype=np.float32)
    else:
        truth = np.zeros_like(lung)

    if os.path.isfile(segmentation_filename):
        with gzip.open(segmentation_filename,'rb') as f:
            outside = np.where(pickle.load(f)>0,0,1)
    else:
        outside = np.where(lung==0,1,0)

    if P.ERODE_SEGMENTATION > 0:
        kernel = skimage.morphology.disk(P.ERODE_SEGMENTATION)
        outside = skimage.morphology.binary_erosion(outside, kernel)

    outside = np.array(outside, dtype=np.float32)

    if P.AUGMENT and not deterministic:
        lung, truth, outside = augment([lung, truth, outside])

    if P.RANDOM_CROP > 0:
        im_x = lung.shape[0]
        im_y = lung.shape[1]
        x = np.random.randint(0, max(1,im_x-P.RANDOM_CROP))
        y = np.random.randint(0, max(1,im_y-P.RANDOM_CROP))

        lung = lung[x:x+P.RANDOM_CROP, y:y+P.RANDOM_CROP]
        truth = truth[x:x+P.RANDOM_CROP, y:y+P.RANDOM_CROP]
        outside = outside[x:x+P.RANDOM_CROP, y:y+P.RANDOM_CROP]

    truth = np.array(np.round(truth),dtype=np.int64)
    outside = np.array(np.round(outside),dtype=np.int64)

    #Set label of outside pixels to -10
    truth = truth - (outside*10)

    lung = lung*(1-outside)

    lung = crop_or_pad(lung, INPUT_SIZE, -1000)
    truth = crop_or_pad(truth, OUTPUT_SIZE, 0)
    outside = crop_or_pad(outside, OUTPUT_SIZE, 0)

    lung = normalize.normalize(lung)
    lung = np.expand_dims(np.expand_dims(lung, axis=0),axis=0)

    if P.ZERO_CENTER:
        lung = lung - P.MEAN_PIXEL

    truth = np.array(np.expand_dims(np.expand_dims(truth, axis=0),axis=0),dtype=np.int64)
    return lung, truth

def crop_or_pad(image, desired_size, pad_value):
    if image.shape[0] < desired_size:
        offset = int(np.ceil((desired_size-image.shape[0])/2))
        image = np.pad(image, offset, 'constant', constant_values=pad_value)

    if image.shape[0] > desired_size:
        offset = (image.shape[0]-desired_size)//2
        image = image[offset:offset+desired_size,offset:offset+desired_size]

    return image

def load_images(filenames, deterministic=False):
    slices = [get_image(filename, deterministic) for filename in filenames]
    lungs, truths = zip(*slices)

    l = np.array(np.concatenate(lungs,axis=0), dtype=np.float32)
    t = np.concatenate(truths,axis=0)

    # Weight the loss by class balancing, classes other than 0 and 1
    # get set to 0 (the background is -10)
    w = loss_weighting.weight_by_class_balance(t, classes=[0,1])

    #Set -1 labels back to label 0
    t = np.clip(t, 0, 100000)

    return l, t, w, filenames

def get_scan_name(filename):
    scan_name = filename.replace('\\','/').split('/')[-1].split('_')[0]
    return scan_name

def train_splits_by_z(filenames, data_resolution=0.5, n_splits=None):
    import pandas as pd

    resolution_of_scan = pd.read_csv('../../data/imagename_zspacing.csv',header=None,names=['filename','spacing'],index_col=False)


    scan_names = set(map(get_scan_name, filenames))
    resolutions = [resolution_of_scan[resolution_of_scan['filename']==scan].iloc[0]['spacing'] for scan in scan_names]

    scan_filenames = []
    for scan in scan_names:
        scan_filenames.append(filter(lambda x: scan in x, filenames))

    if n_splits is None:
        n_splits = np.round(max(resolutions)/data_resolution)

    splits = [ [] for _ in xrange(n_splits)]

    for i, s in enumerate(splits):
        for r, scan, filenames_in_scan in zip(resolutions, scan_names, scan_filenames):
            n = int(np.round(r/data_resolution)) #Amount of splits to divide the filenames over
            s += filenames_in_scan[i%n::n]

    return splits