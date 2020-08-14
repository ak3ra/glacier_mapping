#!/usr/bin/env python
from addict import Dict
from pathlib import Path
from skimage.util.shape import view_as_windows
from .models.frame import Framework
from .models.unet import Unet
from .data.process_slices_funs import postprocess_tile
from torchvision.utils import save_image
import argparse
import geopandas as gpd
import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import os
import pandas as pd
import rasterio
import torch
import yaml


def squash(x):
    return (x - x.min()) / x.ptp()


def append_name(s, args, filetype="png"):
    return f"{s}_{Path(args.input).stem}-{Path(args.model).stem}-{Path(args.process_conf).stem}.{filetype}"


def write_geotiff(y_hat, meta, output_path):
    """
    Write predictions to geotiff

    :param: y_hat A numpy array of predictions.
    """
    # create empty raster with write geographic information
    dst_file = rasterio.open(
        output_path, 'w',
        driver='GTiff',
        height=y_hat.shape[0],
        width=y_hat.shape[1],
        count=y_hat.shape[2],
        dtype=np.float32,
        crs=meta["crs"],
        transform=meta["transform"]
    )

    y_hat = 255.0 * y_hat.astype(np.float32)
    for k in range(y_hat.shape[2]):
        dst_file.write(y_hat[:, :, k], k + 1)


def merge_patches(patches, overlap):
    I, J, _, height, width, channels = patches.shape
    result = np.zeros((I * height, J * width, channels))
    for i in range(I):
        for j in range(J):
            ix_i = i * (height - overlap)
            ix_j = j * (width - overlap)
            result[ix_i : (ix_i + height), ix_j : (ix_j + width)] = patches[i, j]

    return result


def inference(img, model, process_conf, overlap=0, infer_size=1024, device=None):
    """
    inference(tile) -> mask

    :param img: A (unprocessed) numpy array on which to do inference.
    :param model: A pytorch model on which to perform inference. We assume it
      can accept images of size specified in process_conf.slice.size.
    :param process_conf: The path to a yaml file giving the postprocessing
      options. Used to convert the raw tile into the tensor used for inference.
    :return prediction: A segmentation mask of the same width and height as img.
    """
    process_opts = Dict(yaml.safe_load(open(process_conf, "r")))
    channels = process_opts.process_funs.extract_channel.img_channels
    if device is None:
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    # reshape, pad, and slice the input
    size_ = img.shape
    img = pad_to_valid(img)
    img = np.transpose(img, (1, 2, 0))
    slice_size = (
        min(img.shape[0], infer_size),
        min(img.shape[1], infer_size),
        img.shape[2]
    )
    slice_imgs = view_as_windows(img, slice_size, step=slice_size[0] - overlap)

    I, J, _, _, _, _ = slice_imgs.shape
    predictions = np.zeros((I, J, 1, slice_size[0], slice_size[1], 1))
    patches = np.zeros((I, J, 1, slice_size[0], slice_size[1], len(channels)))

    for i in range(I):
        for j in range(J):
            patch, _ = postprocess_tile(slice_imgs[i, j, 0], process_opts.process_funs)
            patches[i, j, :] = patch
            patch = np.transpose(patch, (2, 0, 1))
            patch = torch.from_numpy(patch).float().unsqueeze(0)

            with torch.no_grad():
                patch = patch.to(device)
                y_hat = model(patch).cpu().numpy()
                y_hat = 1 / (1 + np.exp(-y_hat))
                predictions[i, j, 0] = np.transpose(y_hat, (0, 2, 3, 1))

    x = merge_patches(patches, overlap)
    y_hat = merge_patches(predictions, overlap)
    return x[:size_[1], :size_[2], :], y_hat[:size_[1], :size_[2], :]


def next_multiple(size):
    return np.ceil(size / 512) * 512


def pad_to_valid(img):
    size_ = img.shape
    out_rows = next_multiple(size_[1])
    out_cols = next_multiple(size_[2])

    pad_shape = (int(out_rows - size_[1]), int(out_cols - size_[2]))
    return np.pad(img, ((0, 0), (0, pad_shape[0]), (0, pad_shape[1])))



def convert_to_geojson(y_hat, bounds, threshold=0.8):
    y_hat = 1 - y_hat
    contours = skimage.measure.find_contours(y_hat, threshold, fully_connected="high")

    for i in range(len(contours)):
        contours[i] = contours[i][:, [1, 0]]
        contours[i][:, 1] = y_hat.shape[1] - contours[i][:, 1]
        contours[i][:, 0] = bounds[0] + (bounds[2] - bounds[0]) * contours[i][:, 0] / y_hat.shape[0]
        contours[i][:, 1] = bounds[1] + (bounds[3] - bounds[1]) * contours[i][:, 1] / y_hat.shape[1]

    polys = [shapely.geometry.Polygon(a) for a in contours]
    polys = unary_union([p for p in polys if p.area > 4e-6])
    mpoly = shapely.geometry.multipolygon.MultiPolygon(polys)
    mpoly = mpoly.simplify(tolerance=0.0005)
    return gpd.GeoSeries(mpoly).__geo_interface__
