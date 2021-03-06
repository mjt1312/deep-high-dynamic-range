import numpy as np
import tensorflow as tf
from config import *
from typing import List, Tuple
import os
import cv2
import util
import pathlib


def read_exposure(path: str) -> List[float]:
    """Read exposure data from exposures.txt,

    Args:
        path: A str folder path

    Returns:
        A list of exposure times, empty if error
    """
    paths = [f.path for f in os.scandir(path) if f.name.endswith('.txt')]
    if len(paths) < 1:
        print("[read_exposure]: cannot find exposure file")
        return []
    exposure_file_path = paths[0]
    exposures = []
    with open(exposure_file_path) as f:
        for line in f:
            # exposures are specified in exponent representation
            # thus, return exposure times in 2 ** x
            exposures.append(2 ** float(line))
    return exposures


def read_ldr_hdr_images(path: str) -> Tuple[List[np.ndarray], np.ndarray]:
    """
    read 3 LDR images and 1 HDR image

    Args:
        path: a str folder path

    Returns:
        A tuple of
            1: a list of LDR images in np.float32(0-1)
            2: a HDR image in np.float32(0-1)
    """
    paths = [f for f in os.scandir(path)]
    ldr_paths = [x.path for x in paths if x.name.endswith(".tif")]
    # make true we read LDR images based on their exposures
    ldr_paths = sorted(ldr_paths)
    hdr_path = [x.path for x in paths if x.name.endswith(".hdr")]
    if len(ldr_paths) < 3 or len(hdr_path) < 1:
        print("[read_ldr_hdr_images]: cannot find enough ldr/hdr images")
    ldr_imgs = []
    for i in range(3):
        img = util.im2single(cv2.imread(ldr_paths[i], -1))
        # img = util.clamp() TODO: no we really need clamp here
        ldr_imgs.append(img)
    hdr_img = cv2.imread(hdr_path[0], -1)
    return ldr_imgs, hdr_img


def compute_training_examples(ldr_imgs: List[np.ndarray],
                              exposures: List[float], hdr_img: np.ndarray):
    inputs, label = prepare_input_features(
        ldr_imgs, exposures, hdr_img, is_test=False)

    
    # crop out boundary
    inputs = util.crop_img(inputs, CROP_SIZE)
    label = util.crop_img(label, CROP_SIZE)

    # compute patches
    h, w, c = inputs.shape
    num_patches = get_patch_nums(h, w, PATCH_SIZE, STRIDE)

    # generate patches
    input_patches = np.zeros(
        (num_patches *
         NUM_AUGMENT,
         PATCH_SIZE,
         PATCH_SIZE,
         c),
        dtype=np.float32)
    label_patches = np.zeros(
        (num_patches *
         NUM_AUGMENT,
         PATCH_SIZE,
         PATCH_SIZE,
         3),
        dtype=np.float32)

    augument_idx = np.random.permutation(NUM_TOTAL_AUGMENT)

    for i in range(NUM_AUGMENT):
        idx = augument_idx[i]
        augmented_inputs, augmented_labels = augment_data(inputs, label, idx)
        cur_input_patches = get_patches(
            augmented_inputs, PATCH_SIZE, STRIDE)
        cur_label_patches = get_patches(
            augmented_labels, PATCH_SIZE, STRIDE)
        input_patches[i * num_patches: (i + 1) *
                      num_patches, :, :, :] = cur_input_patches
        label_patches[i * num_patches: (i + 1) *
                      num_patches, :, :, :] = cur_label_patches

    selected_subset_idx = select_subset(
        input_patches[:, :, :, 3: 6], PATCH_SIZE)
    input_patches = input_patches[selected_subset_idx, :, :, :]
    label_patches = label_patches[selected_subset_idx, :, :, :]
    return input_patches, label_patches


def compute_test_examples(ldr_imgs: List[np.ndarray],
                          exposures: List[float], hdr_img: np.ndarray):
    inputs, label = prepare_input_features(
        ldr_imgs, exposures, hdr_img, is_test=True)
    inputs = util.crop_img(inputs, CROP_SIZE - BORDER)
    label = util.crop_img(label, CROP_SIZE - BORDER)
    return inputs, label


def prepare_input_features(ldr_imgs: List[np.ndarray], exposures: List[float],
                           hdr_img: np.ndarray, is_test: bool = False):
    """Preprocess LDR/HDR images
    Warp and concate images

    Args:
        ldr_imgs: A list of 3 LDR images
        exposures: A list of 3 corresponding exposure values
        hdr_img: A HDR image
        is_test: Boolean indicate whether change HDR image

    Returns:
        A tuple of
            1: A h * w * 18 matrix of concatenated LDR/converted HDR
            2: A reference HDR image
    """
    # warpped_ldr_imgs = []
    # warpped_ldr_imgs.append(ldr_to_ldr(ldr_imgs[1], exposures[1], exposures[0]))
    # warpped_ldr_imgs.append(ldr_imgs[1])
    # warpped_ldr_imgs.append(ldr_to_ldr(ldr_imgs[1], exposures[1], exposures[2]))

    warpped_ldr_imgs = compute_optical_flow(ldr_imgs, exposures)
    nan_idx0 = np.isnan(warpped_ldr_imgs[0])
    nan_idx2 = np.isnan(warpped_ldr_imgs[2])
    warpped_ldr_imgs[0][nan_idx0] = ldr_to_ldr(
        warpped_ldr_imgs[1][nan_idx0], exposures[1], exposures[0])

    warpped_ldr_imgs[2][nan_idx2] = ldr_to_ldr(
        warpped_ldr_imgs[1][nan_idx2], exposures[1], exposures[2])

    # add clipping to avoid minus value after warpping
    warpped_ldr_imgs[0] = np.clip(warpped_ldr_imgs[0], 0, 1)
    warpped_ldr_imgs[2] = np.clip(warpped_ldr_imgs[2], 0, 1)
    if not is_test:
        dark_ref = np.less(warpped_ldr_imgs[1], 0.5)
        bad_ref = (dark_ref & nan_idx2) | (~dark_ref & nan_idx0)
        # bad_ref = dark_ref
        hdr_img[bad_ref] = ldr_to_hdr(
            warpped_ldr_imgs[1][bad_ref], exposures[1])

    ldr_concate = warpped_ldr_imgs[0]
    for i in range(1, 3):
        ldr_concate = np.concatenate(
            (ldr_concate, warpped_ldr_imgs[i]), axis=2)
    for i in range(3):
        ldr_concate = np.concatenate(
            (ldr_concate, ldr_to_hdr(warpped_ldr_imgs[i], exposures[i])), axis=2)

    return (ldr_concate, hdr_img)


def compute_optical_flow(
        ldr_imgs: List[np.ndarray], exposures: List[float]) -> List[np.ndarray]:
    """compute optical flow and warp images

    Args:
        ldr_imgs: A list of 3 LDR images
        exposures: A list of 3 corresponding exposure values

    Returns:
        A list of 3 images warpped using optical flow

    Notice:
        The middle level exposure image is used
        as reference and not warpped
    """
    exposure_adjusted = []
    exposure_adjusted.append(adjust_exposure(ldr_imgs[0:2], exposures[0:2]))
    exposure_adjusted.append(adjust_exposure(ldr_imgs[1:3], exposures[1:3]))

    flow = []
    flow.append(compute_flow(exposure_adjusted[0][1], exposure_adjusted[0][0]))
    flow.append(compute_flow(exposure_adjusted[1][0], exposure_adjusted[1][1]))

    warpped = []
    warpped.append(warp_using_flow(ldr_imgs[0], flow[0]))
    warpped.append(ldr_imgs[1].copy())
    warpped.append(warp_using_flow(ldr_imgs[2], flow[1]))
    return warpped


def compute_flow(prev: np.ndarray, next: np.ndarray) -> np.ndarray:
    """Compute dense optical flow

    Args:
        prev: Reference image
        next: To be warpped image

    Returns:
        A numpy array for estimated flow

    Notice:
        The algorithm can be replaced as long as
        the interface stays unchanged
    """
    prev_gray = cv2.cvtColor(prev, cv2.COLOR_BGR2GRAY)
    next_gray = cv2.cvtColor(next, cv2.COLOR_BGR2GRAY)
    prev_gray = util.float2int(prev_gray, np.uint16)
    next_gray = util.float2int(next_gray, np.uint16)

    inst = cv2.optflow.createOptFlow_DeepFlow()
    return inst.calc(prev_gray, next_gray, None)
    # return cv2.calcOpticalFlowFarneback(prev_gray, next_gray, flow=None,
    #                                     pyr_scale=0.5, levels=5, winsize=30, iterations=5,
    #                                     poly_n=7, poly_sigma=1.5, flags=0)


def warp_using_flow(img: np.ndarray, flow: np.ndarray) -> np.ndarray:
    """Warp a image using dense optical flow

    Args:
        img: Input image
        flow: Optical flow of the same size

    Returns:
        Warpped image
    """
    h, w, _ = flow.shape
    flow[:, :, 0] += np.arange(w)
    flow[:, :, 1] += np.arange(h)[:, np.newaxis]
    # border value needs to fill all 3 channels
    res = cv2.remap(img, flow, None, cv2.INTER_LINEAR,
                    borderValue=np.array([np.nan, np.nan, np.nan]))
    return res


def get_patch_nums(height: int, width: int, patch_size: int, stride: int):
    """Compute number of patches

    Args:
        height: Image height
        width: Image width

    Returns:
        Number of patches in int
    """
    return int(np.floor((width - patch_size) / stride) + 1) * \
        int(np.floor((height - patch_size) / stride) + 1)


def augment_data(inputs: np.ndarray, label: np.ndarray,
                 idx: int) -> Tuple[np.ndarray, np.ndarray]:
    """Augment the data using specific method

    Args:
        inputs: Concatenated LDR image
        label: HDR image
        idx: Data augmentation method index

    Returns:
        A tuple of
            1: Augmented LDR image
            2: Augmented HDR image
    """
    NUM_COLOR_AUGMENT = 6
    geometric_idx = idx // NUM_COLOR_AUGMENT
    color_idx = idx % NUM_COLOR_AUGMENT

    # since inputs is w * h * 18,
    # loop through all 6 images
    for i in range(6):
        cur_img = inputs[:, :, i * 3: (i + 1) * 3]
        cur_img = geometric_augment(
            color_augment(cur_img, color_idx), geometric_idx)
        if i == 0:
            augmented_inputs = cur_img
        else:
            augmented_inputs = np.concatenate(
                (augmented_inputs, cur_img), axis=2)

    # apply same augmentation on label
    augmented_label = geometric_augment(
        color_augment(label, color_idx), geometric_idx)

    return augmented_inputs, augmented_label


def color_augment(img: np.ndarray, idx: int) -> np.ndarray:
    """Apply color augmentation by changing channel orders

    Args:
        img: Input image
        idx: Int index between [0, 6)

    Returns:
        Reordered image
    """
    orders = [
        [0, 1, 2],
        [0, 2, 1],
        [1, 0, 2],
        [1, 2, 0],
        [2, 1, 0],
        [2, 0, 1]
    ]
    return img[:, :, orders[idx]]


def geometric_augment(img: np.ndarray, idx: int) -> np.ndarray:
    """Apply geometric augmentation by rotation or mirror

    Args:
        img: Input image
        idx: Int index between [0, 8)

    Returns:
        Augmented image
    """
    ops = [
        lambda x: x,
        lambda x: np.fliplr(x),
        lambda x: np.flipud(x),
        lambda x: np.rot90(x, k=2),
        lambda x: np.rot90(x, k=3),
        lambda x: np.fliplr(np.rot90(x, k=3)),
        lambda x: np.flipud(np.rot90(x, k=3)),
        lambda x: np.rot90(x, k=1),
    ]
    return ops[idx](img)


def get_patches(inputs: np.ndarray, patch_size: int,
                stride: int) -> np.ndarray:
    """Get image patches

    Args:
        inputs: Input image
        patch_size: Patch sidelength
        stride: Stride

    Returns:
        Image patches
    """
    h, w, c = inputs.shape
    num_patches = get_patch_nums(h, w, patch_size, stride)
    patches = np.zeros(
        (num_patches,
         patch_size,
         patch_size,
         c),
        dtype=np.float32)
    cnt = 0
    for x in range(0, w - patch_size + 1, stride):
        for y in range(0, h - patch_size + 1, stride):
            patches[cnt, :, :, :] = inputs[y: y +
                                           patch_size, x: x + patch_size, :]
            cnt += 1
    return patches


def select_subset(input_patches: np.ndarray, patch_size: int) -> np.ndarray:
    """Select a subset of image patches
        Only select patches that are overexposed/underexpose(> 50%)

    Args:
        input_patches: Reference image part of input patch
        patch_size: Int patch size

    Returns:
        Selected patches
    """
    threshold = 0.5 * patch_size * patch_size * 3
    lower_bound = 0.2
    upper_bound = 0.8

    idx = np.greater(
        input_patches,
        upper_bound) | np.less(
        input_patches,
        lower_bound)
    idx = np.sum(np.sum(np.sum(idx, axis=3), axis=2), axis=1)

    subset_idx = np.where(idx > threshold)[0]
    return subset_idx


def write_training_examples(
        inputs: np.ndarray, label: np.ndarray, path: str, filename: str):
    n = inputs.shape[0]
    filename = filename.split('/')[-1]

    if not os.path.exists(path):
        pathlib.Path(path).mkdir(parents=True, exist_ok=True)

    filename = path + "Scene" + filename + "_{}.tfrecords"

    filename_suffix_cnt = 0
    write_cnt = 0
    while (write_cnt < n):
        cur_filename = filename.format(filename_suffix_cnt)
        print(f"[writing_training_examples]: writing {cur_filename}")
        with tf.io.TFRecordWriter(cur_filename) as writer:
            for i in range(write_cnt, min(write_cnt + 500, n)):
                cur_inputs_bytes = inputs[i, :, :, :].tostring()
                cur_label_bytes = label[i, :, :, :].tostring()

                example = serialize_training_example(
                    cur_inputs_bytes, cur_label_bytes)
                writer.write(example)
        write_cnt += 500
        filename_suffix_cnt += 1


def serialize_training_example(inputs, label):
    feature = {
        "inputs": tf_records_bytes_feature(inputs),
        "label": tf_records_bytes_feature(label)
    }

    example_proto = tf.train.Example(
        features=tf.train.Features(
            feature=feature))
    return example_proto.SerializeToString()


def read_training_tf_record(serialized_example):
    feature = {
        "inputs": tf.io.FixedLenFeature((), tf.string),
        "label": tf.io.FixedLenFeature((), tf.string),
    }

    example = tf.io.parse_single_example(serialized_example, feature)
    inputs = tf.reshape(
        tf.io.decode_raw(
            example['inputs'], out_type=tf.float32), [
            40, 40, 18])
    label = tf.reshape(
        tf.io.decode_raw(
            example['label'], out_type=tf.float32), [
            40, 40, 3])
    return inputs, label


def read_training_examples(files):
    tf_record_dataset = tf.data.TFRecordDataset(files)
    parsed_dataset = tf_record_dataset.map(read_training_tf_record)
    return parsed_dataset


def write_test_examples(
        inputs: np.ndarray, label: np.ndarray, path: str, filename: str):
    filename = filename.split('/')[-1]
    if not os.path.exists(path):
        pathlib.Path(path).mkdir(parents=True, exist_ok=True)
    filename = path + "Scene" + filename + ".tfrecords"
    print(f"[writing_training_examples]: writing {filename}")
    with tf.io.TFRecordWriter(filename) as writer:
        height, width, _ = inputs.shape
        print(f"[writing_training_examples]: {height} {width}")
        inputs_bytes = inputs.tostring()
        label_bytes = label.tostring()

        example = serialize_test_example(
            height, width, inputs_bytes, label_bytes)
        writer.write(example)


def serialize_test_example(height, width, inputs, label):
    feature = {
        "height": tf_records_int64_feature(height),
        "width": tf_records_int64_feature(width),
        "inputs": tf_records_bytes_feature(inputs),
        "label": tf_records_bytes_feature(label)
    }
    example_proto = tf.train.Example(
        features=tf.train.Features(
            feature=feature))
    return example_proto.SerializeToString()


def read_test_tf_record(serialized_example):
    feature = {
        'height': tf.io.FixedLenFeature((), tf.int64,),
        'width': tf.io.FixedLenFeature((), tf.int64,),
        "inputs": tf.io.FixedLenFeature((), tf.string),
        "label": tf.io.FixedLenFeature((), tf.string),
    }

    example = tf.io.parse_single_example(serialized_example, feature)
    height = example['height']
    width = example['width']
    inputs = tf.reshape(
        tf.io.decode_raw(
            example['inputs'], out_type=tf.float32), [
            height, width, 18])
    label = tf.reshape(
        tf.io.decode_raw(
            example['label'], out_type=tf.float32), [
            height, width, 3])
    return inputs, label


def read_test_examples(files):
    tf_record_dataset = tf.data.TFRecordDataset(files)
    parsed_dataset = tf_record_dataset.map(read_test_tf_record)
    return parsed_dataset


def adjust_exposure(imgs: List[np.ndarray],
                    exposures: List[float]) -> List[np.ndarray]:
    """Adjust image exposure

    Args:
        imgs: A list of images
        exposures: A list of corresponding exposure values

    Returns:
        A list of adjusted images

    Notice:
        The function raise the image with lower exposure to the
        higher one to achieve brightness constancy
    """
    adjusted = []
    max_exposure = max(exposures)
    for i in range(len(imgs)):
        adjusted.append(ldr_to_ldr(imgs[i], exposures[i], max_exposure))
    return adjusted


def ldr_to_ldr(ldr_img: np.ndarray, exposure_src: float,
               exposure_dst: float) -> np.ndarray:
    """Map a LDR image to a LDR image with different exposure

    Args:
        ldr_img: A LDR image
        exposure_src: Exposure value of the input image
        exposure_dst: Exposure value to raised to

    Returns:
        A image with exposure raised/unchanged(exposure_src == exposure_dst)
    """

    return hdr_to_ldr(ldr_to_hdr(ldr_img, exposure_src), exposure_dst)


def ldr_to_hdr(ldr_img: np.ndarray, exposure: float) -> np.ndarray:
    """Map a LDR image to a HDR image

    Args:
        ldr_img: A LDR image
        exposure: Exposure value of the input image

    Returns:
        A HDR image
    """
    return np.power(ldr_img, GAMMA) / exposure


def hdr_to_ldr(hdr_img: np.ndarray, exposure: float) -> np.ndarray:
    """Map a HDR image to a LDR image

    Args:
        ldr_img: A HDR image
        exposure: Target exposure value

    Returns:
        A LDR image
    """
    hdr_img = hdr_img.astype(np.float32) * exposure
    hdr_img = np.clip(hdr_img, 0, 1)
    return np.power(hdr_img, (1 / GAMMA))


def tf_records_bytes_feature(value):
    if isinstance(value, type(tf.constant(0))):
        value = value.numpy()
    return tf.train.Feature(bytes_list=tf.train.BytesList(value=[value]))


def tf_records_int64_feature(value):
    return tf.train.Feature(int64_list=tf.train.Int64List(value=[value]))
