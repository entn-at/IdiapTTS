#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright (c) 2019 Idiap Research Institute, http://www.idiap.ch/
# Written by Bastian Schnell <bastian.schnell@idiap.ch>
#

"""Module description:
   Train a model to predict the position of atoms and generate LF0 from them.
   Combine LF0 data with external mgc and bap data to synthesize audio.
"""


# System imports.
import logging
import math
import sys
import numpy as np
import os

# Third-party imports.

# Local source tree imports.
from idiaptts.src.model_trainers.ModelTrainer import ModelTrainer
from idiaptts.src.data_preparation.questions.QuestionLabelGen import QuestionLabelGen
from idiaptts.src.data_preparation.wcad.AtomLabelGen import AtomLabelGen
from idiaptts.src.data_preparation.world.WorldFeatLabelGen import WorldFeatLabelGen
from idiaptts.src.neural_networks.pytorch.loss.WeightedNonzeroMSELoss import WeightedNonzeroMSELoss
from idiaptts.src.data_preparation.PyTorchLabelGensDataset import PyTorchLabelGensDataset
from idiaptts.src.DataPlotter import DataPlotter
from idiaptts.misc.utils import interpolate_lin
from idiaptts.src.model_trainers.AcousticModelTrainer import AcousticModelTrainer
from idiaptts.src.Synthesiser import Synthesiser


class AtomModelTrainer(ModelTrainer):
    """
    Implementation of a ModelTrainer for the generation of acoustic data through atom prediction.
    Output labels for atoms have dimension: T x |thetas| x 2 (amp, theta).

    Use question labels as input and extracted wcad atoms as output. Synthesize audio from model
    output by generating F0 from atoms. MGC and BAP is either generated by a pre-trained acoustic
    model or loaded from the original extracted files.
    """
    logger = logging.getLogger(__name__)

    def __init__(self, wcad_root, dir_atom_labels, dir_question_labels, id_list, thetas, k,
                 num_questions, hparams=None):
        """Default constructor.

        :param wcad_root:               Path to main directory of wcad.
        :param dir_atom_labels:         Path to directory that contains the .atom files.
        :param dir_question_labels:     Path to directory that contains the .questions files.
        :param id_list:                 List containing all ids. Subset is taken as test set.
        :param thetas:                  List of theta values.
        :param k:                       K value of atoms.
        :param num_questions:           Expected number of questions in question labels.
        :param hparams:                 Hyper-parameter container.
        """
        if hparams is None:
            hparams = self.create_hparams()
            hparams.out_dir = os.path.curdir

        # Write missing default parameters.
        if hparams.variable_sequence_length_train is None:
            hparams.variable_sequence_length_train = hparams.batch_size_train > 1
        if hparams.variable_sequence_length_test is None:
            hparams.variable_sequence_length_test = hparams.batch_size_test > 1
        if hparams.synth_dir is None:
            hparams.synth_dir = os.path.join(hparams.out_dir, "synth")

        # If the weight for unvoiced frames is not given, compute it to get equal weights.
        non_zero_occurrence = min(0.99, 0.02 / len(thetas))
        zero_occurrence = 1 - non_zero_occurrence
        if not hasattr(hparams, "weight_zero"):
            hparams.add_hparam("weight_non_zero", 1 / non_zero_occurrence)
            hparams.add_hparam("weight_zero", 1 / zero_occurrence)
        elif hparams.weight_zero is None:
            hparams.weight_non_zero = 1 / non_zero_occurrence
            hparams.weight_zero = 1 / zero_occurrence

        super().__init__(id_list, hparams)

        self.InputGen = QuestionLabelGen(dir_question_labels, num_questions)
        self.InputGen.get_normalisation_params(dir_question_labels, hparams.input_norm_params_file_prefix)

        self.OutputGen = AtomLabelGen(wcad_root, dir_atom_labels, thetas, k, hparams.frame_size_ms)
        self.OutputGen.get_normalisation_params(dir_atom_labels, hparams.output_norm_params_file_prefix)

        self.dataset_train = PyTorchLabelGensDataset(self.id_list_train, self.InputGen, self.OutputGen, hparams, match_lengths=True)
        self.dataset_val = PyTorchLabelGensDataset(self.id_list_val, self.InputGen, self.OutputGen, hparams, match_lengths=True)

        if self.loss_function is None:
            self.loss_function = WeightedNonzeroMSELoss(hparams.use_gpu, hparams.weight_zero, hparams.weight_non_zero,
                                                        size_average=False, reduce=False)
        if hparams.scheduler_type == "default":
            hparams.scheduler_type = "Plateau"
            hparams.add_hparams(
                plateau_patience=10,
                plateau_factor=0.5,
                plateau_verbose=True)

    @staticmethod
    def create_hparams(hparams_string=None, verbose=False):
        hparams = ModelTrainer.create_hparams(hparams_string, verbose=False)

        hparams.add_hparams(thetas=None,
                            k=None,
                            min_atom_amp=0.3,
                            num_questions=None
                            )

        if verbose:
            logging.info(hparams.get_debug_string())

        return hparams

    def gen_figure_from_output(self, id_name, labels, hidden, hparams):

        if labels.ndim < 2:
            labels = np.expand_dims(labels, axis=1)
        labels_post = self.OutputGen.postprocess_sample(labels, identify_peaks=True, peak_range=100)
        lf0 = self.OutputGen.labels_to_lf0(labels_post, hparams.k)
        lf0, vuv = interpolate_lin(lf0)
        vuv = vuv.astype(np.bool)

        # Load original lf0 and vuv.
        world_dir = hparams.world_dir if hasattr(hparams, "world_dir") and hparams.world_dir is not None\
                                      else os.path.join(self.OutputGen.dir_labels, self.dir_extracted_acoustic_features)
        org_labels = WorldFeatLabelGen.load_sample(id_name, world_dir, num_coded_sps=hparams.num_coded_sps)
        _, original_lf0, original_vuv, _ = WorldFeatLabelGen.convert_to_world_features(org_labels, num_coded_sps=hparams.num_coded_sps)
        original_lf0, _ = interpolate_lin(original_lf0)
        original_vuv = original_vuv.astype(np.bool)

        phrase_curve = np.fromfile(os.path.join(self.OutputGen.dir_labels, id_name + self.OutputGen.ext_phrase),
                                   dtype=np.float32).reshape(-1, 1)
        original_lf0 -= phrase_curve
        len_diff = len(original_lf0) - len(lf0)
        original_lf0 = WorldFeatLabelGen.trim_end_sample(original_lf0, int(len_diff / 2.0))
        original_lf0 = WorldFeatLabelGen.trim_end_sample(original_lf0, int(len_diff / 2.0) + 1, reverse=True)

        org_labels = self.OutputGen.load_sample(id_name, self.OutputGen.dir_labels, len(hparams.thetas))
        org_labels = self.OutputGen.trim_end_sample(org_labels, int(len_diff / 2.0))
        org_labels = self.OutputGen.trim_end_sample(org_labels, int(len_diff / 2.0) + 1, reverse=True)
        org_atoms = self.OutputGen.labels_to_atoms(org_labels, k=hparams.k, frame_size=hparams.frame_size_ms)

        # Get a data plotter.
        net_name = os.path.basename(hparams.model_name)
        filename = str(os.path.join(hparams.out_dir, id_name + '.' + net_name))
        plotter = DataPlotter()
        plotter.set_title(id_name + " - " + net_name)

        graphs_output = list()
        grid_idx = 0
        for idx in reversed(range(labels.shape[1])):
            graphs_output.append((labels[:, idx], r'$\theta$=' + "{0:.3f}".format(hparams.thetas[idx])))
        plotter.set_label(grid_idx=grid_idx, xlabel='frames [' + str(hparams.frame_size_ms) + ' ms]', ylabel='NN output')
        plotter.set_data_list(grid_idx=grid_idx, data_list=graphs_output)
        # plotter.set_lim(grid_idx=0, ymin=-1.8, ymax=1.8)

        grid_idx += 1
        graphs_peaks = list()
        for idx in reversed(range(labels_post.shape[1])):
            graphs_peaks.append((labels_post[:, idx, 0],))
        plotter.set_label(grid_idx=grid_idx, xlabel='frames [' + str(hparams.frame_size_ms) + ' ms]', ylabel='NN post-processed')
        plotter.set_data_list(grid_idx=grid_idx, data_list=graphs_peaks)
        plotter.set_area_list(grid_idx=grid_idx, area_list=[(np.invert(vuv), '0.8', 1.0)])
        plotter.set_lim(grid_idx=grid_idx, ymin=-1.8, ymax=1.8)

        grid_idx += 1
        graphs_target = list()
        for idx in reversed(range(org_labels.shape[1])):
            graphs_target.append((org_labels[:, idx, 0],))
        plotter.set_label(grid_idx=grid_idx, xlabel='frames [' + str(hparams.frame_size_ms) + ' ms]', ylabel='target')
        plotter.set_data_list(grid_idx=grid_idx, data_list=graphs_target)
        plotter.set_area_list(grid_idx=grid_idx, area_list=[(np.invert(original_vuv), '0.8', 1.0)])
        plotter.set_lim(grid_idx=grid_idx, ymin=-1.8, ymax=1.8)

        grid_idx += 1
        output_atoms = AtomLabelGen.labels_to_atoms(labels_post, hparams.k, hparams.frame_size_ms, amp_threshold=hparams.min_atom_amp)
        wcad_lf0 = AtomLabelGen.atoms_to_lf0(org_atoms, len(labels))
        output_lf0 = AtomLabelGen.atoms_to_lf0(output_atoms, len(labels))
        graphs_lf0 = list()
        graphs_lf0.append((wcad_lf0, "wcad lf0"))
        graphs_lf0.append((original_lf0, "org lf0"))
        graphs_lf0.append((output_lf0, "predicted lf0"))
        plotter.set_data_list(grid_idx=grid_idx, data_list=graphs_lf0)
        plotter.set_area_list(grid_idx=grid_idx, area_list=[(np.invert(original_vuv), '0.8', 1.0)])
        plotter.set_label(grid_idx=grid_idx, xlabel='frames [' + str(hparams.frame_size_ms) + ' ms]', ylabel='lf0')
        amp_lim = max(np.max(np.abs(wcad_lf0)), np.max(np.abs(output_lf0))) * 1.1
        plotter.set_lim(grid_idx=grid_idx, ymin=-amp_lim, ymax=amp_lim)
        plotter.set_linestyles(grid_idx=grid_idx, linestyles=[':', '--', '-'])

        # plotter.set_lim(xmin=300, xmax=1100)
        plotter.gen_plot()
        plotter.save_to_file(filename + ".BASE" + hparams.gen_figure_ext)

    def get_recon_from_synth_output(self, synth_output, hparams):
        """Reconstruct LF0 from atoms."""

        # Transform output to GammaAtoms.
        recon_dict = dict()
        for id_name, label in synth_output.items():
            if len(label.shape) == 2:
                label = np.expand_dims(label, axis=1)

            atoms = self.OutputGen.labels_to_atoms(label, k=hparams.k, frame_size=hparams.frame_size_ms, amp_threshold=hparams.min_atom_amp)
            reconstruction = self.OutputGen.atoms_to_lf0(atoms, num_frames=len(label))

            # Add extracted phrase.
            phrase_curve = np.fromfile(os.path.join(self.OutputGen.dir_labels, id_name + self.OutputGen.ext_phrase),
                                       dtype=np.float32)[:len(reconstruction)]
            reconstruction[:len(phrase_curve)] += phrase_curve
            reconstruction[reconstruction <= math.log(WorldFeatLabelGen.f0_silence_threshold)] = WorldFeatLabelGen.lf0_zero

            recon_dict[id_name] = reconstruction

        return recon_dict

    def get_phrase_curve(self, id_name):
        return np.fromfile(os.path.join(self.OutputGen.dir_labels, id_name + self.OutputGen.ext_phrase),
                           dtype=np.float32).reshape(-1, 1)

    def compute_score(self, dict_outputs_post, dict_hiddens, hparams):

        # Get data for comparision.
        dict_original_post = self.load_extracted_audio_features(dict_outputs_post, hparams)

        f0_rmse = 0.0
        f0_rmse_max_id = "None"
        f0_rmse_max = 0.0
        for id_name, labels in dict_outputs_post.items():
            output_lf0 = AtomLabelGen.labels_to_lf0(labels,
                                                    k=hparams.k,
                                                    frame_size=hparams.frame_size_ms,
                                                    amp_threshold=hparams.min_atom_amp)

            # Get data for comparision.
            org_lf0 = dict_original_post[id_name][:, hparams.num_coded_sps]
            org_vuv = dict_original_post[id_name][:, hparams.num_coded_sps + 1]
            phrase_curve = self.get_phrase_curve(id_name)

            # Compute f0 from lf0.
            org_f0 = (np.exp(org_lf0.squeeze()) * org_vuv)[:len(output_lf0)]  # Fix minor negligible length mismatch.
            output_f0 = np.exp(output_lf0 + phrase_curve[:len(output_lf0)].squeeze()) * org_vuv[:len(output_lf0)]

            # Compute RMSE, keep track of worst RMSE.
            f0_mse = (org_f0 - output_f0) ** 2
            current_f0_rmse = math.sqrt(f0_mse.sum() / org_vuv.sum())
            if current_f0_rmse > f0_rmse_max:
                f0_rmse_max_id = id_name
                f0_rmse_max = current_f0_rmse
            f0_rmse += current_f0_rmse

        f0_rmse /= len(dict_outputs_post)
        self.logger.info("Worst F0 RMSE: " + f0_rmse_max_id + " {:4.2f}Hz".format(f0_rmse_max))
        self.logger.info("Benchmark score: F0 RMSE " + "{:4.2f}Hz".format(f0_rmse))

        return f0_rmse

    def load_extracted_audio_features(self, synth_output, hparams):
        """Load the audio features extracted from audio."""
        self.logger.info("Load extracted mgc, lf0, vuv, bap data.")

        org_output = dict()
        for id_name in synth_output.keys():
            world_dir = hparams.world_dir if hasattr(hparams, "world_dir") and hparams.world_dir is not None\
                                          else os.path.realpath(os.path.join(self.OutputGen.dir_labels, self.dir_extracted_acoustic_features))
            org_output[id_name] = WorldFeatLabelGen.load_sample(id_name, world_dir, add_deltas=False, num_coded_sps=hparams.num_coded_sps)  # Load extracted data.

        return org_output

    def generate_audio_features(self, id_list, hparams):  # TODO: This function is untested.
        """
        Generate mgc, vuv and bap data with an acoustic model.
        The name of the acoustic model is saved in hparams.synth_acoustic_model_path and given in the constructor.
        If the synth_acoustic_model_path is 'None' this method will not be called but the method
        load_extracted_audio_features, which reloads the original data extracted from the audio.

        If you want to generate audio directly from wcad atom extraction, uncomment the first block
        in the get_recon_from_synth_output method.

        Detailed execution process:
        This method reuses the synth method of the ModelTrainer base class. It overwrites the internal
        f_synthesize method and the OutputGen to accomplish the audio generation. Both are restored
        after finishing the generation. The base class synth method loads the acoustic model network
        by its name and forwards the question labels for each utterance in the id_list. At the
        end the method calls the f_synthesize method. Therefore the f_synthesize method is overwritten
        by the save_audio_features which saves the generate output mgc, vuv and bap files in the
        self.synth_dir folder.
        """
        self.logger.info("Generate mgc, vuv and bap with " + hparams.synth_acoustic_model_path)

        acoustic_model_hparams = AcousticModelTrainer.create_hparams()
        acoustic_model_hparams.model_name = os.path.basename(hparams.synth_acoustic_model_path)
        acoustic_model_hparams.model_path = hparams.synth_acoustic_model_path
        acoustic_model_handler = AcousticModelTrainer(acoustic_model_hparams)

        org_model_handler = self.model_handler
        self.model_handler = acoustic_model_handler

        # Switch f_synthesize method and OutputGen for mgc, vuv and bap creation.
        # f_synthesize is called at the end of synth.
        self.f_synthesize = self.save_audio_features
        org_output_gen = self.OutputGen
        self.OutputGen = self.AudioGen

        # Explicitly synthesize with acoustic_model_name.
        # This method calls f_synthesize at the end which will save the mgc, vuv and bap.
        self.synth(hparams, id_list)

        # Switch back to atom creation.
        self.f_synthesize = self.synthesize
        self.OutputGen = org_output_gen
        self.model_handler = org_model_handler

    def synthesize(self, id_list, synth_output, hparams):
        """This method should be overwritten by sub classes."""
        # Create lf0 from atoms of output and get other acoustic features either by loading the original labels or by
        # generating them with the model at hparams.synth_acoustic_model_path.
        full_output = self.run_atom_synth(id_list, synth_output, hparams)
        # Run the WORLD synthesizer.
        Synthesiser.run_world_synth(full_output, hparams)

    def synth_ref_wcad(self, file_id_list, hparams):
        synth_output = dict()
        # Load extracted atoms.
        for id_name in file_id_list:
            synth_output[id_name] = AtomLabelGen.load_sample(id_name, self.OutputGen.dir_labels,
                                                             len(hparams.thetas))

        full_output = self.run_atom_synth(file_id_list, synth_output, hparams)

        # Add identifier to suffix.
        old_synth_file_suffix = hparams.synth_file_suffix
        hparams.synth_file_suffix += "_wcad_ref"

        # Run the WORLD synthesizer.
        Synthesiser.run_world_synth(full_output, hparams)

        # Restore identifier.
        hparams.synth_file_suffix = old_synth_file_suffix

    def synth_phrase(self, file_id_list, hparams):
        # Create reference audio files containing only the vocoder degradation.
        self.logger.info("Synthesise phrase curve for [{0}].".format(", ".join([id_name for id_name in file_id_list])))

        # Create an empty dictionary which can be filled with extracted audio features.
        synth_output = dict()
        for id_name in file_id_list:
            synth_output[id_name] = None
        # Fill dictionary with extracted audio features.
        full_output = self.load_extracted_audio_features(synth_output, hparams)

        # Override the lf0 component by the phrase curve.
        for id_name in file_id_list:
            labels = full_output[id_name]
            phrase_curve = np.fromfile(os.path.join(self.OutputGen.dir_labels, id_name + self.OutputGen.ext_phrase),
                                       dtype=np.float32)[:len(full_output[id_name])]
            labels[:, -3] = phrase_curve[:len(labels)]

        # Add identifier to suffix.
        old_synth_file_suffix = hparams.synth_file_suffix
        hparams.synth_file_suffix += '_phrase'

        # Run the vocoder.
        ModelTrainer.synthesize(self, file_id_list, full_output, hparams)

        # Restore identifier.
        hparams.synth_file_suffix = old_synth_file_suffix

    def run_atom_synth(self, file_id_list, synth_output, hparams):
        """
        Reconstruct lf0, get mgc and bap data, and store all in files in self.synth_dir.
        """

        # Get mgc, vuv and bap data either through a trained acoustic model or from data extracted from the audio.
        if hparams.synth_acoustic_model_path is None:
            full_output = self.load_extracted_audio_features(synth_output, hparams)
        else:
            self.logger.warning("This method is untested.")
            full_output = self.generate_audio_features(file_id_list, hparams)

        # Reconstruct lf0 from generated atoms and write it to synth output.
        recon_dict = self.get_recon_from_synth_output(synth_output, hparams)
        for id_name, lf0 in recon_dict.items():
            full_sample = full_output[id_name]
            len_diff = len(full_sample) - len(lf0)
            full_sample = WorldFeatLabelGen.trim_end_sample(full_sample, int(len_diff / 2), reverse=True)
            full_sample = WorldFeatLabelGen.trim_end_sample(full_sample, len_diff - int(len_diff / 2))
            vuv = np.ones(lf0.shape)
            vuv[lf0 <= math.log(WorldFeatLabelGen.f0_silence_threshold)] = 0.0
            full_sample[:, hparams.num_coded_sps] = lf0
            full_sample[:, hparams.num_coded_sps + 1] = vuv

        return full_output
