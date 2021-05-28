#!/usr/bin/env python3
"""
WRITEME [outline the steps in the pipeline]

This pipeline preprocesses CoughVid 2.0 data.
The target is the self-reported multiclass diagnosis.

The idea is that each of these tasks should has a separate working
directory in _workdir. We remove it only when the entire pipeline
is done. This is safer, even tho it uses more disk space.
(The principle here is we don't remove any working dirs during
processing.)

When hacking on this file, consider only enabling one Task at a
time in __main__.

Also keep in mind that if the S3 caching is enabled, you
will always just retrieve your S3 cache instead of running
the pipeline.

TODO:
* We need all the files in the README.md created for each dataset
(task.json, train.csv, etc.).
* After downloading from Zenodo, check that the zipfile has the
correct MD5.
* Would be nice to compute the 50% and 75% percentile audio length
to the metadata.
"""

import glob
import os
import shutil
import subprocess

import luigi
import numpy as np
import pandas as pd
import soundfile as sf
from slugify import slugify
from tqdm.auto import tqdm

import config.coughvid as config
import util.audio as audio_util
from util.luigi import (
    download_file,
    ensure_dir,
    filename_to_int_hash,
    new_basedir,
    which_set,
    WorkTask,
)


class DownloadCorpus(WorkTask):
    @property
    def name(self):
        return type(self).__name__

    def run(self):
        # TODO: Change the working dir
        download_file(
            "https://zenodo.org/record/4498364/files/public_dataset.zip",
            os.path.join(self.workdir, "corpus.zip"),
        )
        with self.output().open("w") as outfile:
            pass

    @property
    def stage_number(self) -> int:
        return 0


class ExtractCorpus(WorkTask):
    def requires(self):
        return DownloadCorpus()

    @property
    def name(self):
        return type(self).__name__

    def run(self):
        # Location of zip file to extract. Figure this out before changing
        # the working directory.
        corpus_zip = os.path.realpath(
            os.path.join(self.requires().workdir, "corpus.zip")
        )
        subprocess.check_output(["unzip", "-o", corpus_zip, "-d", self.workdir])

        with self.output().open("w") as outfile:
            pass


class SubsampleCorpus(WorkTask):
    """
    Subsample the corpus so that we have the appropriate number of
    audio files.

    Additionally, since the upstream data files might be in subfolders,
    we slugify them here so that we just have one flat directory.
    (TODO: Double check this works.)

    A destructive way of implementing this task is that it removes
    extraneous files, rather than copying them to the next task's
    directory. However, one safety convention we apply is doing
    non-destructive work, one working directory per task (or set
    of related tasks of the same class, like resampling with different
    SRs).
    """

    def requires(self):
        return ExtractCorpus()

    @property
    def name(self):
        return type(self).__name__

    def run(self):
        # Really coughvid? webm + ogg?
        audiofiles = list(
            glob.glob(os.path.join(self.requires().workdir, "public_dataset/*.webm"))
            + glob.glob(os.path.join(self.requires().workdir, "public_dataset/*.ogg"))
        )

        # Make sure we found audio files to work with
        if len(audiofiles) == 0:
            raise RuntimeError(f"No audio files found in {self.requires().workdir}")

        # Deterministically randomly sort all files by their hash
        audiofiles.sort(key=lambda filename: filename_to_int_hash(filename))
        if len(audiofiles) > config.MAX_FILES_PER_CORPUS:
            print(
                "%d audio files in corpus, keeping only %d"
                % (len(audiofiles), config.MAX_FILES_PER_CORPUS)
            )
        # Save diskspace using symlinks
        for audiofile in audiofiles[: config.MAX_FILES_PER_CORPUS]:
            # Extract the audio filename, excluding the old working
            # directory, but including all subfolders
            newaudiofile = os.path.join(
                self.workdir,
                os.path.split(
                    slugify(os.path.relpath(audiofile, self.requires().workdir))
                )[0],
                # This is pretty gnarly but we do it to not slugify the filename extension
                os.path.split(audiofile)[1],
            )
            # Make sure we don't have any duplicates
            assert not os.path.exists(newaudiofile)
            os.symlink(os.path.realpath(audiofile), newaudiofile)
        with self.output().open("w") as outfile:
            pass


class ToMonoWavCorpus(WorkTask):
    """
    Convert all audio to WAV files using Sox.
    We convert to mono, and also ensure that all files are the same length.
    """

    def requires(self):
        return SubsampleCorpus()

    @property
    def name(self):
        return type(self).__name__

    def run(self):
        audiofiles = list(
            glob.glob(os.path.join(self.requires().workdir, "*.webm"))
            + glob.glob(os.path.join(self.requires().workdir, "*.ogg"))
        )
        for audiofile in tqdm(audiofiles):
            newaudiofile = new_basedir(
                os.path.splitext(audiofile)[0] + ".wav", self.workdir
            )
            audio_util.convert_to_mono_wav(audiofile, newaudiofile)
        with self.output().open("w") as outfile:
            pass


class SubsampleMetadata(WorkTask):
    """
    Subsample the metadata (labels), and normalize it the CSV format
    described in README.md.
    """

    def requires(self):
        """
        This depends upon ToMonoWavCorpus to get the final WAV
        filenames (which should not change after this point,
        besides being sorted into {sr}/{partition}/ subdirectories),
        and the metadata in ExtractCorpus.
        """
        return [ToMonoWavCorpus(), ExtractCorpus()]

    @property
    def name(self):
        return type(self).__name__

    def run(self):
        # Unfortunately, this somewhat fragilely depends upon the order
        # of self.requires

        audiofiles = list(glob.glob(os.path.join(self.requires()[0].workdir, "*.wav")))

        # Make sure we found audio files to work with
        if len(audiofiles) == 0:
            raise RuntimeError(f"No audio files found in {self.requires()[0].workdir}")

        labeldf = pd.read_csv(
            os.path.join(
                self.requires()[1].workdir, "public_dataset/metadata_compiled.csv"
            )
        )
        labeldf["uuid"] = labeldf["uuid"] + ".wav"
        audiodf = pd.DataFrame(
            [os.path.split(a)[1] for a in audiofiles], columns=["uuid"]
        )
        # Sanity check there aren't duplicates in the metadata's
        # list of audiofiles
        assert len(audiofiles) == len(audiodf.drop_duplicates())

        sublabeldf = labeldf.merge(audiodf, on="uuid")
        sublabeldf.to_csv(
            os.path.join(self.workdir, "metadata.csv"),
            columns=["uuid", "status"],
            index=False,
            header=False,
        )

        with self.output().open("w") as outfile:
            pass


class EnsureLengthCorpus(WorkTask):
    """
    Ensure all WAV files are a particular length.
    There might be a one-liner in ffmpeg that we can convert to WAV
    and ensure the file length at the same time.
    """

    def requires(self):
        return ToMonoWavCorpus()

    @property
    def name(self):
        return type(self).__name__

    def run(self):
        for audiofile in tqdm(
            list(glob.glob(os.path.join(self.requires().workdir, "*.wav")))
        ):
            x, sr = sf.read(audiofile)
            target_length_samples = int(round(sr * config.SAMPLE_LENGTH_SECONDS))
            # Convert to mono
            if x.ndim == 2:
                x = np.mean(x, axis=1)
            assert x.ndim == 1, "Audio should be mono"
            # Trim if necessary
            x = x[:target_length_samples]
            if len(x) < target_length_samples:
                x = np.hstack([x, np.zeros(target_length_samples - len(x))])
            assert len(x) == target_length_samples
            newaudiofile = new_basedir(audiofile, self.workdir)
            sf.write(newaudiofile, x, sr)
        with self.output().open("w") as outfile:
            pass


class SplitTrainTestCorpus(WorkTask):
    """
    If there is already a train/test split, we use that.
    Otherwise we deterministically
    """

    def requires(self):
        return EnsureLengthCorpus()

    @property
    def name(self):
        return type(self).__name__

    def run(self):
        for audiofile in tqdm(list(glob.glob(f"{self.requires().workdir}/*.wav"))):
            partition = which_set(
                audiofile, validation_percentage=0.0, testing_percentage=10.0
            )
            partition_dir = f"{self.workdir}/{partition}"
            ensure_dir(partition_dir)
            newaudiofile = new_basedir(audiofile, partition_dir)
            os.symlink(os.path.realpath(audiofile), newaudiofile)
        with self.output().open("w") as outfile:
            pass


class SplitTrainTestMetadata(WorkTask):
    """
    Split the metadata into train / test.
    """

    def requires(self):
        """
        This depends upon SplitTrainTestCorpus to get the partitioned WAV
        filenames, and the subsampled metadata in SubsampleMetadata.
        """
        return [SplitTrainTestCorpus(), SubsampleMetadata()]

    @property
    def name(self):
        return type(self).__name__

    def run(self):
        # Unfortunately, this somewhat fragilely depends upon the order
        # of self.requires

        # Might also want "val" for some corpora
        splittotal = 0
        origtotal = None
        for partition in ["train", "test"]:
            audiofiles = list(
                glob.glob(os.path.join(self.requires()[0].workdir, partition, "*.wav"))
            )

            # Make sure we found audio files to work with
            if len(audiofiles) == 0:
                raise RuntimeError(
                    f"No audio files found in {self.requires()[0].workdir}/{partition}"
                )

            labeldf = pd.read_csv(
                os.path.join(self.requires()[1].workdir, "metadata.csv"),
                header=None,
                names=["filename", "label"],
            )
            audiodf = pd.DataFrame(
                [os.path.split(a)[1] for a in audiofiles], columns=["filename"]
            )
            assert len(audiofiles) == len(audiodf.drop_duplicates())

            sublabeldf = labeldf.merge(audiodf, on="filename")

            origtotal = len(labeldf)
            splittotal += len(sublabeldf)
            sublabeldf.to_csv(
                os.path.join(self.workdir, f"{partition}.csv"),
                columns=["filename", "label"],
                index=False,
                header=False,
            )

        assert origtotal == splittotal

        with self.output().open("w") as outfile:
            pass


class ResampleSubCorpus(WorkTask):
    sr = luigi.IntParameter()
    partition = luigi.Parameter()

    def requires(self):
        return SplitTrainTestCorpus()

    @property
    def name(self):
        return type(self).__name__

    # Since these tasks have parameters but share the same working
    # directory and name, we postpend the parameters to the output
    # filename, so we can track if one ResampleSubCorpus task finished
    # but others didn't.
    def output(self):
        return luigi.LocalTarget(
            "_workdir/%02d-%s-%d-%s.done"
            % (self.stage_number, self.name, self.sr, self.partition)
        )

    def run(self):
        resample_dir = f"{self.workdir}/{self.sr}/{self.partition}/"
        ensure_dir(resample_dir)
        for audiofile in tqdm(
            list(glob.glob(f"{self.requires().workdir}/{self.partition}/*.wav"))
        ):
            resampled_audiofile = new_basedir(audiofile, resample_dir)
            audio_util.resample_wav(audiofile, resampled_audiofile, self.sr)
        with self.output().open("w") as outfile:
            pass


class FinalizeCorpus(WorkTask):
    """
    Create a final corpus, no longer in _workdir but in the top-level
    at directory config.TASKNAME.
    """

    def requires(self):
        return [
            ResampleSubCorpus(sr, partition)
            for sr in config.SAMPLE_RATES
            for partition in ["train", "test", "val"]
        ] + [SplitTrainTestMetadata()]

    @property
    def name(self):
        return type(self).__name__

    # We overwrite workdir here, because we want the output to be
    # the finalized top-level task directory
    @property
    def workdir(self):
        return config.TASKNAME

    def run(self):
        if os.path.exists(self.workdir):
            shutil.rmtree(self.workdir)
        # Fragilely depends upon the order of the requires
        shutil.copytree(self.requires()[0].workdir, self.workdir)
        # Might also want "val" for some corpora
        for partition in ["train", "test"]:
            shutil.copy(
                os.path.join(self.requires()[-1].workdir, f"{partition}.csv"),
                self.workdir,
            )
        with self.output().open("w") as outfile:
            pass


if __name__ == "__main__":
    print("max_files_per_corpus = %d" % config.MAX_FILES_PER_CORPUS)
    ensure_dir("_workdir")
    luigi.build([FinalizeCorpus()], workers=config.NUM_WORKERS, local_scheduler=True)
