import re
import os
import numpy as np
import matplotlib.pyplot as plt
import random
from collections import defaultdict
import scipy.signal
import json
import copy
import glob
import sys
import pickle
import string, shutil
import logging
import pytorch_lightning as pl
from functools import lru_cache
from copy import copy

import librosa
from scipy.io import loadmat

import torch
from data_utils import (
    load_audio,
    get_emg_features,
    FeatureNormalizer,
    phoneme_inventory,
    read_phonemes,
    TextTransform,
)
from torch.utils.data import DataLoader

from dataloaders import cache_dataset

# DATA_FOLDER    = '/scratch/GaddyPaper'
DATA_FOLDER = os.path.join(os.environ["SCRATCH"], "GaddyPaper")
# project_folder = '/home/tyler/code/silent_speech'
project_folder = "/home/users/tbenst/code/silent_speech/"

REMOVE_CHANNELS = []
SILENT_DATA_DIRECTORIES = [f"{DATA_FOLDER}/emg_data/silent_parallel_data"]
# VOICED_DATA_DIRECTORIES = [f'{DATA_FOLDER}/emg_data/voiced_parallel_data',
#                                               f'{DATA_FOLDER}/emg_data/nonparallel_data']
# we include voiced parallel data in each example of silent data
# this is for convenience in the dataloader
VOICED_DATA_DIRECTORIES = [f"{DATA_FOLDER}/emg_data/nonparallel_data"]
PARALLEL_DATA_DIRECTORIES = [f"{DATA_FOLDER}/emg_data/voiced_parallel_data"]
TESTSET_FILE = f"{project_folder}/testset_largedev.json"
TEXT_ALIGN_DIRECTORY = f"{DATA_FOLDER}/text_alignments"


def remove_drift(signal, fs):
    b, a = scipy.signal.butter(3, 2, "highpass", fs=fs)
    return scipy.signal.filtfilt(b, a, signal)


def notch(signal, freq, sample_frequency):
    b, a = scipy.signal.iirnotch(freq, 30, sample_frequency)
    return scipy.signal.filtfilt(b, a, signal)


def notch_harmonics(signal, freq, sample_frequency):
    for harmonic in range(1, 8):
        signal = notch(signal, freq * harmonic, sample_frequency)
    return signal


def subsample(signal, new_freq, old_freq):
    times = np.arange(len(signal)) / old_freq
    sample_times = np.arange(0, times[-1], 1 / new_freq)
    result = np.interp(sample_times, times, signal)
    return result


def apply_to_all(function, signal_array, *args, **kwargs):
    results = []
    for i in range(signal_array.shape[1]):
        results.append(function(signal_array[:, i], *args, **kwargs))
    return np.stack(results, 1)


def load_utterance(
    base_dir,
    index,
    limit_length=False,
    debug=False,
    text_align_directory=None,
    returnRaw=True,
):
    # I'm not totally sure about the point of returnRaw, perhaps for aligning with audio..?
    index = int(index)
    raw_emg = np.load(os.path.join(base_dir, f"{index}_emg.npy"))
    before = os.path.join(base_dir, f"{index-1}_emg.npy")
    after = os.path.join(base_dir, f"{index+1}_emg.npy")
    if os.path.exists(before):
        raw_emg_before = np.load(before)
    else:
        raw_emg_before = np.zeros([0, raw_emg.shape[1]])
    if os.path.exists(after):
        raw_emg_after = np.load(after)
    else:
        raw_emg_after = np.zeros([0, raw_emg.shape[1]])

    x = np.concatenate([raw_emg_before, raw_emg, raw_emg_after], 0)
    x = apply_to_all(notch_harmonics, x, 60, 1000)
    x = apply_to_all(remove_drift, x, 1000)
    x = x[raw_emg_before.shape[0] : x.shape[0] - raw_emg_after.shape[0], :]

    # TODO/INFO: why do we subsample
    # I think this is misnamed, it's actually downsampling so that audio
    # and emg have the same number of frames
    if returnRaw:
        emg_orig = apply_to_all(subsample, x, 689.06, 1000)
    else:
        emg_orig = x.copy()
    x = apply_to_all(subsample, x, 516.79, 1000)
    emg = x

    for c in REMOVE_CHANNELS:
        emg[:, int(c)] = 0
        emg_orig[:, int(c)] = 0

    emg_features = get_emg_features(emg)

    mfccs = load_audio(
        os.path.join(base_dir, f"{index}_audio_clean.flac"),
        max_frames=min(emg_features.shape[0], 800 if limit_length else float("inf")),
    )

    if emg_features.shape[0] > mfccs.shape[0]:
        emg_features = emg_features[: mfccs.shape[0], :]
    assert emg_features.shape[0] == mfccs.shape[0]
    emg = emg[6 : 6 + 6 * emg_features.shape[0], :]
    emg_orig = emg_orig[8 : 8 + 8 * emg_features.shape[0], :]
    assert emg.shape[0] == emg_features.shape[0] * 6

    with open(os.path.join(base_dir, f"{index}_info.json")) as f:
        info = json.load(f)

    sess = os.path.basename(base_dir)
    tg_fname = f"{text_align_directory}/{sess}/{sess}_{index}_audio.TextGrid"
    if os.path.exists(tg_fname):
        phonemes = read_phonemes(tg_fname, mfccs.shape[0])
    else:
        phonemes = np.zeros(mfccs.shape[0], dtype=np.int64) + phoneme_inventory.index(
            "sil"
        )

    return (
        mfccs,
        emg_features,
        info["text"],
        (info["book"], info["sentence_index"]),
        phonemes,
        emg_orig.astype(np.float32),
    )


class EMGDirectory(object):
    def __init__(
        self,
        session_index,
        directory,
        silent,
        exclude_from_testset=False,
        exclude=False,
    ):
        self.session_index = session_index
        self.directory = directory
        self.silent = silent
        self.exclude_from_testset = exclude_from_testset
        self.exclude = exclude

    def __lt__(self, other):
        return self.session_index < other.session_index

    def __repr__(self):
        return self.directory


class DistributedSizeAwareSampler(torch.utils.data.Sampler):
    """Sample batches of examples from the dataset,
    ensuring that each batch fits within max_len."""

    def __init__(
        self,
        lengths: np.ndarray,
        max_len: int = 256000,
        shuffle: bool = True,
        seed: int = 20230819,
        epoch: int = 0,
        num_replicas: int = 1,
        constant_num_batches: bool = True,
    ):
        self.lengths = lengths
        self.max_len = max_len
        self.shuffle = shuffle
        self.seed = seed
        self.epoch = epoch

        # for distributed training
        rank_key = "RANK" if "RANK" in os.environ else "LOCAL_RANK"
        self.rank = int(os.environ[rank_key]) if rank_key in os.environ else 0
        self.num_replicas = num_replicas

        self.constant_num_batches = False
        if constant_num_batches:
            self.hardcode_len = self.min_len(200)  # assume 200 epochs
            self.constant_num_batches = True
            logging.warning(
                f"Hard coding len to {self.hardcode_len} as hack to get pytorch lightning to work"
            )

    def __iter__(self):
        return self.iter_batches(self.rank)

    def iter_batches(self):
        g = torch.Generator()
        g.manual_seed(self.seed + self.epoch)
        indices = torch.randperm(len(self.dataset), generator=g).tolist()
        indices = indices[self.rank :: self.num_replicas]
        batch = []
        batch_length = 0
        for idx in indices:
            length = self.lengths[idx]
            if length > self.max_len:
                logging.warning(
                    f"Warning: example {idx} cannot fit within desired batch length"
                )
            if length + batch_length > self.max_len:
                yield batch
                batch = []
                batch_length = 0
            batch.append(idx)
            batch_length += length
        # dropping last incomplete batch

    def set_epoch(self, epoch: int):
        self.epoch = epoch

    def min_len(self, num_epochs: int):
        """Minimum number of batches in any epoch."""
        cur_epoch = self.epoch
        min_length = np.inf
        # minimum per epoch
        for epoch in range(num_epochs):
            self.set_epoch(epoch)
            # minimum per GPU
            for rank in range(self.num_replicas):
                N = len(list(self.iter_batches(rank)))
                if N < min_length:
                    min_length = N
        self.set_epoch(cur_epoch)
        return min_length

    def __len__(self):
        "Return approximate number of batches per epoch"
        # https://github.com/Lightning-AI/lightning/issues/18023
        if self.constant_num_batches:
            return self.hardcode_len
        else:
            return len(iter(self))


_local_regex = re.compile(r"^.*(emg_data/.*)$")


def local_path_for_audio_file(audio_file):
    "Strip absolute path from audio file name, leaving only the local path for portability."
    m = _local_regex.match(audio_file)
    if m is None:
        raise ValueError(f"Could not parse local path for {audio_file=}")
    return m[1]


_dir_regex = re.compile(r"^(.*)/processed_data/.+/session_\d+/\d+.mat$")


def parent_dir_for_preprocessed_mat(mat_file):
    "Return absolute path Gaddy data dir from mat file."
    m = _dir_regex.match(mat_file)
    if m is None:
        raise ValueError(f"Could not parse parent directory for {mat_file=}")
    return m[1]


def lookup_preprocessed_emg_length(example):
    audio_file = loadmat(example)["audio_file"][0]
    fn = local_path_for_audio_file(audio_file)
    parent = parent_dir_for_preprocessed_mat(example)
    fp = os.path.join(parent, fn)
    json_file = fp.split("_audio_clean")[0] + "_info.json"

    with open(json_file) as f:
        info = json.load(f)

    if not np.any([l in string.ascii_letters for l in info["text"]]):
        return False

    length = sum([emg_len for emg_len, _, _ in info["chunks"]])
    return length


class PreprocessedSizeAwareSampler(torch.utils.data.Sampler):
    def __init__(self, emg_dataset, max_len, shuffle=True):
        self.dataset = emg_dataset
        self.max_len = max_len
        self.shuffle = shuffle

        self.lengths = self.dataset.lengths
        self.approx_len = int(np.ceil(np.array(self.lengths)).sum() / max_len)

    def __iter__(self):
        indices = list(range(len(self.dataset)))
        if self.shuffle:
            random.shuffle(indices)
        batch = []
        batch_length = 0
        for idx in indices:
            length = self.lengths[idx]
            if length > self.max_len:
                logging.warning(
                    f"Warning: example {idx} cannot fit within desired batch length"
                )
            if length + batch_length > self.max_len:
                yield batch
                batch = []
                batch_length = 0
            batch.append(idx)
            batch_length += length
        # dropping last incomplete batch

    def __len__(self):
        return self.approx_len


class EMGDataset(torch.utils.data.Dataset):
    def __init__(
        self,
        base_dir=None,
        normalizers_file=None,
        limit_length=False,
        dev=False,
        test=False,
        no_testset=False,
        no_normalizers=False,
        returnRaw=True,
        togglePhones=False,
    ):
        self.text_align_directory = TEXT_ALIGN_DIRECTORY

        if no_testset:
            devset = []
            testset = []
        else:
            with open(TESTSET_FILE) as f:
                testset_json = json.load(f)
                devset = testset_json["dev"]
                testset = testset_json["test"]

        directories = []
        if base_dir is not None:
            directories.append(EMGDirectory(0, base_dir, False))
        else:
            for sd in SILENT_DATA_DIRECTORIES:
                for session_dir in sorted(os.listdir(sd)):
                    directories.append(
                        EMGDirectory(
                            len(directories), os.path.join(sd, session_dir), True
                        )
                    )

            has_silent = len(SILENT_DATA_DIRECTORIES) > 0
            for vd in VOICED_DATA_DIRECTORIES:
                for session_dir in sorted(os.listdir(vd)):
                    directories.append(
                        EMGDirectory(
                            len(directories),
                            os.path.join(vd, session_dir),
                            False,
                            exclude_from_testset=has_silent,
                        )
                    )

            # these are also voiced, however we will include them in the silent data
            # so we want to avoid adding them twice, but still need to add them to the
            # voiced_data_locations
            for pd in PARALLEL_DATA_DIRECTORIES:
                for session_dir in sorted(os.listdir(pd)):
                    directories.append(
                        EMGDirectory(
                            len(directories),
                            os.path.join(pd, session_dir),
                            False,
                            exclude=True,
                        )
                    )

        self.example_indices = []
        self.lengths = []
        # map from book/sentence_index to directory_info/index
        self.voiced_data_locations = {}
        for directory_info in directories:
            for fname in os.listdir(directory_info.directory):
                m = re.match(r"(\d+)_info.json", fname)
                if m is not None:
                    idx_str = m.group(1)
                    with open(os.path.join(directory_info.directory, fname)) as f:
                        info = json.load(f)
                        if (
                            info["sentence_index"] >= 0
                        ):  # boundary clips of silence are marked -1
                            location_in_testset = [
                                info["book"],
                                info["sentence_index"],
                            ] in testset
                            location_in_devset = [
                                info["book"],
                                info["sentence_index"],
                            ] in devset
                            if (
                                (  # test data
                                    test
                                    and location_in_testset
                                    and not directory_info.exclude_from_testset
                                    and not directory_info.exclude
                                )
                                or (  # validation data
                                    dev
                                    and location_in_devset
                                    and not directory_info.exclude_from_testset
                                    and not directory_info.exclude
                                )
                                or (  # training data
                                    not test
                                    and not dev
                                    and not location_in_testset
                                    and not location_in_devset
                                    and not directory_info.exclude
                                )
                            ):
                                self.example_indices.append(
                                    (directory_info, int(idx_str))
                                )
                                self.lengths.append(
                                    sum([emg_len for emg_len, _, _ in info["chunks"]])
                                )

                            if not directory_info.silent:
                                location = (info["book"], info["sentence_index"])
                                self.voiced_data_locations[location] = (
                                    directory_info,
                                    int(idx_str),
                                )

        self.example_indices.sort()
        random.seed(0)
        random.shuffle(self.example_indices)

        self.no_normalizers = no_normalizers
        if not self.no_normalizers:
            with open(normalizers_file, "rb") as f:
                self.mfcc_norm, self.emg_norm = pickle.load(f)

        sample_mfccs, sample_emg, _, _, _, _ = load_utterance(
            self.example_indices[0][0].directory, self.example_indices[0][1]
        )
        self.num_speech_features = sample_mfccs.shape[1]
        self.num_features = sample_emg.shape[1]
        self.limit_length = limit_length
        self.num_sessions = len(directories)
        self.returnRaw = returnRaw

        self.text_transform = TextTransform(togglePhones=togglePhones)

    def silent_subset(self):
        result = copy(self)
        silent_indices = []
        for example in self.example_indices:
            if example[0].silent:
                silent_indices.append(example)
        result.example_indices = silent_indices
        return result

    def subset(self, fraction):
        result = copy(self)
        result.example_indices = self.example_indices[
            : int(fraction * len(self.example_indices))
        ]
        return result

    def __len__(self):
        return len(self.example_indices)

    @lru_cache(maxsize=None)
    def __getitem__(self, i):
        directory_info, idx = self.example_indices[i]
        mfccs, emg, text, book_location, phonemes, raw_emg = load_utterance(
            directory_info.directory,
            idx,
            self.limit_length,
            text_align_directory=self.text_align_directory,
            returnRaw=self.returnRaw,
        )
        raw_emg = raw_emg / 20
        raw_emg = 50 * np.tanh(raw_emg / 50.0)

        if not self.no_normalizers:
            mfccs = self.mfcc_norm.normalize(mfccs)
            emg = self.emg_norm.normalize(emg)
            emg = 8 * np.tanh(emg / 8.0)

        session_ids = np.full(
            emg.shape[0], directory_info.session_index, dtype=np.int64
        )
        audio_file = f"{directory_info.directory}/{idx}_audio_clean.flac"

        text_int = np.array(self.text_transform.text_to_int(text), dtype=np.int64)

        result = {
            "audio_features": torch.from_numpy(mfccs),
            "emg": torch.from_numpy(emg),
            "text": text,
            "text_int": torch.from_numpy(text_int),
            "file_label": idx,
            "session_ids": torch.from_numpy(session_ids),
            "book_location": book_location,
            "silent": directory_info.silent,
            "raw_emg": torch.from_numpy(raw_emg),
        }

        if directory_info.silent:
            voiced_directory, voiced_idx = self.voiced_data_locations[book_location]
            voiced_mfccs, voiced_emg, _, _, phonemes, voiced_raw_emg = load_utterance(
                voiced_directory.directory,
                voiced_idx,
                self.limit_length,
                text_align_directory=self.text_align_directory,
                returnRaw=self.returnRaw,
            )
            voiced_raw_emg = voiced_raw_emg / 20
            voiced_raw_emg = 50 * np.tanh(voiced_raw_emg / 50.0)

            # if not self.no_normalizers:
            #     #voiced_mfccs = self.mfcc_norm.normalize(voiced_mfccs)  # HACKY WORKAROUND - AVOID MAKING MFCCS
            #     voiced_emg = self.emg_norm.normalize(voiced_emg)
            #     voiced_emg = 8*np.tanh(voiced_emg/8.)

            result["parallel_voiced_audio_features"] = torch.from_numpy(voiced_mfccs)
            result["parallel_voiced_raw_emg"] = torch.from_numpy(voiced_raw_emg)

            audio_file = f"{voiced_directory.directory}/{voiced_idx}_audio_clean.flac"

        result["phonemes"] = torch.from_numpy(
            phonemes
        )  # either from this example if vocalized or aligned example if silent
        result["audio_file"] = audio_file

        return result

    @staticmethod
    def collate_raw(batch):
        batch_size = len(batch)
        audio_features = []
        audio_feature_lengths = []
        parallel_emg = []
        for ex in batch:
            if ex["silent"]:
                audio_features.append(ex["parallel_voiced_audio_features"])
                audio_feature_lengths.append(
                    ex["parallel_voiced_audio_features"].shape[0]
                )
                parallel_emg.append(ex["parallel_voiced_raw_emg"])
            else:
                audio_features.append(ex["audio_features"])
                audio_feature_lengths.append(ex["audio_features"].shape[0])
                parallel_emg.append(np.zeros(1))
        phonemes = [ex["phonemes"] for ex in batch]
        emg = [ex["emg"] for ex in batch]
        raw_emg = [ex["raw_emg"] for ex in batch]
        session_ids = [ex["session_ids"] for ex in batch]
        lengths = [ex["emg"].shape[0] for ex in batch]
        silent = [ex["silent"] for ex in batch]
        text = [ex["text"] for ex in batch]
        text_ints = [ex["text_int"] for ex in batch]
        text_lengths = [ex["text_int"].shape[0] for ex in batch]

        result = {
            "audio_features": audio_features,
            "audio_feature_lengths": audio_feature_lengths,
            "emg": emg,
            "raw_emg": raw_emg,
            "parallel_voiced_raw_emg": parallel_emg,
            "phonemes": phonemes,
            "session_ids": session_ids,
            "lengths": lengths,
            "silent": silent,
            "text": text,
            "text_int": text_ints,
            "text_int_lengths": text_lengths,
        }
        return result


# TODO: remove pin_memory as dataloader should handle. make sure no performance hit
class PreprocessedEMGDataset(torch.utils.data.Dataset):
    def __init__(
        self,
        base_dir=None,
        train=False,
        dev=False,
        test=False,
        limit_length=False,
        pin_memory=True,
        no_normalizers=False,
        togglePhones=False,
        device=None,
        normalizers_file=None,
    ):
        self.togglePhones = togglePhones

        files = list()
        if train:
            partition_files = glob.glob(os.path.join(base_dir, "train/") + "*/*.mat")
            # print(f'Adding {len(partition_files)} to dataset.')
            files.extend(partition_files)

        if dev:
            partition_files = glob.glob(os.path.join(base_dir, "dev/") + "*/*.mat")
            # print(f'Adding {len(partition_files)} to dataset.')
            files.extend(partition_files)

        if test:
            partition_files = glob.glob(os.path.join(base_dir, "test/") + "*/*.mat")
            # print(f'Adding {len(partition_files)} to dataset.')
            files.extend(partition_files)

        self.example_indices = files
        self.train = train
        self.dev = dev
        self.test = test
        self.pin_memory = pin_memory
        self.device = device

        self.example_indices.sort()
        np.random.seed(0)
        np.random.shuffle(self.example_indices)

        mat = loadmat(self.example_indices[0])
        self.num_speech_features = mat["audio_features"].shape[1]
        self.num_features = mat["emg"].shape[1]
        self.limit_length = limit_length
        self.num_sessions = len(
            glob.glob(os.path.join(base_dir, "train/") + "*session_*/")
        )

        self.text_transform = TextTransform(togglePhones=self.togglePhones)

        self.no_normalizers = no_normalizers
        if not self.no_normalizers:
            self.mfcc_norm, self.emg_norm = pickle.load(open(normalizers_file, "rb"))

        self.lengths = [
            lookup_preprocessed_emg_length(ex) for ex in self.example_indices
        ]

    def silent_subset(self):
        result = copy(self)
        silent_indices = []
        for example in self.example_indices:
            if example[0].silent:
                silent_indices.append(example)
        result.example_indices = silent_indices
        return result

    def subset(self, fraction):
        result = copy(self)
        result.example_indices = self.example_indices[
            : int(fraction * len(self.example_indices))
        ]
        return result

    def __len__(self):
        return len(self.example_indices)

    @lru_cache(maxsize=None)
    def __getitem__(self, i):
        result = loadmat(self.example_indices[i])

        if self.pin_memory:
            keys = [
                "audio_features",
                "emg",
                "text",
                "session_ids",
                "raw_emg",
                "phonemes",
                "parallel_voiced_emg",
                "parallel_voiced_audio_features",
            ]
            for key in keys:
                try:
                    result[key] = torch.tensor(result[key].squeeze())
                except:
                    continue

        result["text_int"] = torch.tensor(
            np.array(self.text_transform.text_to_int(result["text"][0]), dtype=np.int64)
        )

        return result

    @staticmethod
    def collate_raw(batch):
        audio_features = []
        audio_feature_lengths = []
        parallel_emg = []
        for ex in batch:
            if ex["silent"]:
                audio_features.append(ex["parallel_voiced_audio_features"])
                audio_feature_lengths.append(
                    ex["parallel_voiced_audio_features"].shape[0]
                )
                parallel_emg.append(ex["parallel_voiced_emg"])
            else:
                audio_features.append(ex["audio_features"])
                audio_feature_lengths.append(ex["audio_features"].shape[0])
                parallel_emg.append(np.zeros(1))

        phonemes = [ex["phonemes"] for ex in batch]
        emg = [ex["emg"] for ex in batch]
        raw_emg = [ex["raw_emg"] for ex in batch]
        session_ids = [ex["session_ids"] for ex in batch]
        lengths = [ex["emg"].shape[0] for ex in batch]
        silent = [ex["silent"] for ex in batch]
        text = [ex["text"] for ex in batch]
        text_int = [ex["text_int"] for ex in batch]
        int_lengths = [ex["text_int"].shape[0] for ex in batch]

        result = {
            "audio_features": audio_features,
            "audio_feature_lengths": audio_feature_lengths,
            "emg": emg,
            "raw_emg": raw_emg,
            "parallel_voiced_emg": parallel_emg,
            "phonemes": phonemes,
            "session_ids": session_ids,
            "lengths": lengths,
            "silent": silent,
            "text": text,
            "text_int": text_int,
            "text_int_lengths": int_lengths,
        }

        return result


class EMGDataModule(pl.LightningDataModule):
    def __init__(
        self,
        base_dir,
        togglePhones,
        normalizers_file,
        drop_last=None,
        max_len=128000,
        num_workers=0,
        batch_sampler=True,
        shuffle=None,
        batch_size=None,
        collate_fn=None,
        pin_memory=True,
    ) -> None:
        super().__init__()
        self.train = cache_dataset(
            os.path.join(base_dir, "2024-01-20a_emg_train.pkl"), EMGDataset
        )(
            base_dir=None,
            dev=False,
            test=False,
            returnRaw=True,
            togglePhones=togglePhones,
            normalizers_file=normalizers_file,
        )
        self.val = cache_dataset(
            os.path.join(base_dir, "2024-01-20a_emg_val.pkl"), EMGDataset
        )(
            base_dir=None,
            dev=True,
            test=False,
            returnRaw=True,
            togglePhones=togglePhones,
            normalizers_file=normalizers_file,
        )

        self.test = cache_dataset(
            os.path.join(base_dir, "2024-01-20a_emg_test.pkl"), EMGDataset
        )(
            base_dir=None,
            dev=False,
            test=True,
            returnRaw=True,
            togglePhones=togglePhones,
            normalizers_file=normalizers_file,
        )
        #             batch_size=None, collate_fn=None, DatasetClass=PreprocessedEMGDataset,
        #             pin_memory=True) -> None:
        # super().__init__()
        # self.train = DatasetClass(base_dir = base_dir, train = True, dev = False, test = False,
        #                                 togglePhones = togglePhones, normalizers_file = normalizers_file)
        # self.val   = DatasetClass(base_dir = base_dir, train = False, dev = True, test = False,
        #                                 togglePhones = togglePhones, normalizers_file = normalizers_file)

        # self.test = DatasetClass(base_dir = base_dir, train = False, dev = False, test = True,
        #                             togglePhones = togglePhones, normalizers_file = normalizers_file)
        self.num_workers = num_workers
        self.max_len = max_len
        self.val_test_batch_sampler = False
        self.batch_size = (
            batch_size if batch_size is not None and batch_size > 0 else 1
        )  # can't be None
        self.batch_sampler = batch_sampler
        self.drop_last = drop_last
        self.collate_fn = collate_fn
        self.shuffle = shuffle
        self.pin_memory = pin_memory

    def train_dataloader(self):
        collate_fn = (
            self.collate_fn if self.collate_fn is not None else self.train.collate_raw
        )
        batch_sampler = (
            PreprocessedSizeAwareSampler(self.train, self.max_len)
            if self.batch_sampler
            else None
        )
        if batch_sampler:
            loader = DataLoader(
                self.train,
                collate_fn=collate_fn,
                num_workers=self.num_workers,
                pin_memory=self.pin_memory,
                batch_sampler=batch_sampler,
            )
        else:
            loader = DataLoader(
                self.train,
                collate_fn=collate_fn,
                shuffle=self.shuffle,
                drop_last=self.drop_last,
                num_workers=self.num_workers,
                batch_size=self.batch_size,
                pin_memory=self.pin_memory,
                batch_sampler=batch_sampler,
            )

        return loader

    def val_dataloader(self):
        collate_fn = (
            self.collate_fn if self.collate_fn is not None else self.val.collate_raw
        )

        if self.val_test_batch_sampler:
            batch_sampler = (
                PreprocessedSizeAwareSampler(self.val, self.max_len)
                if self.batch_sampler
                else None
            )
            loader = DataLoader(
                self.val,
                collate_fn=collate_fn,
                num_workers=self.num_workers,
                pin_memory=self.pin_memory,
                batch_sampler=batch_sampler,
            )
        else:
            loader = DataLoader(
                self.val,
                collate_fn=collate_fn,
                num_workers=self.num_workers,
                batch_size=self.batch_size,
                pin_memory=self.pin_memory,
            )
        return loader

    def test_dataloader(self):
        collate_fn = (
            self.collate_fn if self.collate_fn is not None else self.test.collate_raw
        )

        if self.val_test_batch_sampler:
            loader = DataLoader(
                self.test,
                collate_fn=collate_fn,
                num_workers=self.num_workers,
                pin_memory=self.pin_memory,
                batch_sampler=PreprocessedSizeAwareSampler(
                    self.test, self.max_len, shuffle=False
                ),
            )
        else:
            loader = DataLoader(
                self.test,
                collate_fn=collate_fn,
                num_workers=self.num_workers,
                batch_size=self.batch_size,
                pin_memory=self.pin_memory,
            )
        return loader


def make_normalizers(normalizers_file):
    dataset = EMGDataset(no_normalizers=True)
    mfcc_samples = []
    emg_samples = []
    for d in dataset:
        mfcc_samples.append(d["audio_features"])
        emg_samples.append(d["emg"])
        if len(emg_samples) > 50:
            break
    mfcc_norm = FeatureNormalizer(mfcc_samples, share_scale=True)
    emg_norm = FeatureNormalizer(emg_samples, share_scale=False)
    with open(normalizers_file, "wb") as f:
        pickle.dump((mfcc_norm, emg_norm), f)

def ensure_folder_on_scratch(src, dst):
    "Check if folder exists on scratch, otherwise copy. Return new path."
    assert os.path.isdir(src)
    split_path = src.split(os.sep)
    name = split_path[-1] if split_path[-1] != "" else split_path[-2]
    out = os.path.join(dst, name)
    try:
        if not os.path.isdir(out):
            shutil.copytree(src, out)
    except FileExistsError:
        # If the directory was created between the check and the copy attempt,
        # ignore the error. this resolves the potential race condition.
        pass
    return out
