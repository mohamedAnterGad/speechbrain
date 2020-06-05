#!/usr/bin/env python3
import os
import sys
import speechbrain as sb
import speechbrain.data_io.wer as wer_io
import speechbrain.utils.edit_distance as edit_distance
from speechbrain.data_io.data_io import convert_index_to_lab
from speechbrain.decoders.ctc import ctc_greedy_decode
from speechbrain.decoders.transducer import decode_batch
from speechbrain.data_io.data_io import put_bos_token
from speechbrain.decoders.decoders import undo_padding
from speechbrain.utils.checkpoints import ckpt_recency
from speechbrain.utils.train_logger import summarize_error_rate

# This hack needed to import data preparation script from ..
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(current_dir))
from timit_prepare import prepare_timit  # noqa E402

# Load hyperparameters file with command-line overrides
params_file, overrides = sb.core.parse_arguments(sys.argv[1:])
with open(params_file) as fin:
    params = sb.yaml.load_extended_yaml(fin, overrides)

# Create experiment directory
sb.core.create_experiment_directory(
    experiment_directory=params.output_folder,
    params_to_save=params_file,
    overrides=overrides,
)


# Define training procedure
class ASR(sb.core.Brain):
    def compute_forward(self, x, y, stage="train", init_params=False):
        id, wavs, lens = x
        wavs, lens = wavs.to(params.device), lens.to(params.device)
        if hasattr(params, "augmentation") and stage == "train":
            wavs = params.augmentation(wavs, lens, init_params)
        feats = params.compute_features(wavs, init_params)
        feats = params.normalize(feats, lens)
        # Transcription network: input-output dependency
        TN_output = params.encoder_crdnn(feats, init_params=init_params)
        TN_output = params.encoder_lin(TN_output, init_params)
        if stage == "train":
            _, targets, _ = y
            targets = targets.to(params.device)
            # Prediction network: output-output dependency
            # Generate input seq for PN
            decoder_input = put_bos_token(targets, bos_index=params.blank_index)
            PN_output = params.decoder_embedding(decoder_input, init_params)
            PN_output, _ = params.decoder_gru(
                PN_output, init_params=init_params
            )
            PN_output = params.decoder_lin(PN_output, init_params)
            # Joint the networks
            joint = params.Tjoint(
                TN_output.unsqueeze(2),
                PN_output.unsqueeze(1),
                init_params=init_params,
            )
            # projection layer
            outputs = params.output(joint, init_params)
        else:
            outputs = decode_batch(
                TN_output,
                [
                    params.decoder_embedding,
                    params.decoder_gru,
                    params.decoder_lin,
                ],
                params.Tjoint,
                [params.output],
                params.blank_index,
            )
        outputs = params.log_softmax(outputs)
        return outputs, lens

    def compute_objectives(self, predictions, targets, stage="train"):
        predictions, lens = predictions
        ids, phns, phn_lens = targets
        if stage == "train":
            loss = params.compute_cost(
                predictions,
                phns.to(params.device).long(),
                lens.to(params.device),
                phn_lens.to(params.device),
            )

        stats = {}
        if stage != "train":
            predictions = predictions.squeeze(2)
            loss = -predictions.max(dim=-1)[0].sum(dim=-1).mean()
            ind2lab = params.train_loader.label_dict["phn"]["index2lab"]
            sequence = ctc_greedy_decode(
                predictions, lens, blank_id=params.blank_index
            )
            sequence = convert_index_to_lab(sequence, ind2lab)
            phns = undo_padding(phns, phn_lens)
            phns = convert_index_to_lab(phns, ind2lab)
            per_stats = edit_distance.wer_details_for_batch(
                ids, phns, sequence, compute_alignments=True
            )
            stats["PER"] = per_stats

        return loss, stats

    def on_epoch_end(self, epoch, train_stats, valid_stats=None):
        per = summarize_error_rate(valid_stats["PER"])
        old_lr, new_lr = params.lr_annealing([params.optimizer], epoch, per)
        epoch_stats = {"epoch": epoch, "lr": old_lr}
        params.train_logger.log_stats(epoch_stats, train_stats, valid_stats)

        params.checkpointer.save_and_keep_only(
            meta={"PER": per},
            importance_keys=[ckpt_recency, lambda c: -c.meta["PER"]],
        )

    def fit_batch(self, batch):
        inputs, targets = batch
        predictions = self.compute_forward(inputs, targets)
        loss, stats = self.compute_objectives(predictions, targets)
        loss.backward()
        self.optimizer.step()
        self.optimizer.zero_grad()
        stats["loss"] = loss.detach()
        return stats

    def evaluate_batch(self, batch, stage="test"):
        inputs, targets = batch
        out = self.compute_forward(inputs, None, stage=stage)
        loss, stats = self.compute_objectives(out, targets, stage=stage)
        stats["loss"] = loss.detach()
        return stats


# Prepare data
prepare_timit(
    data_folder=params.data_folder,
    splits=["train", "dev", "test"],
    save_folder=params.data_folder,
)
train_set = params.train_loader()
valid_set = params.valid_loader()
first_x, first_y = next(iter(train_set))

# Modules are passed to optimizer and have train/eval called on them
modules = [
    params.encoder_crdnn,
    params.encoder_lin,
    params.decoder_gru,
    params.decoder_lin,
    params.joint_lin,
    params.output,
]
if hasattr(params, "augmentation"):
    modules.append(params.augmentation)

# Create brain object for training
asr_brain = ASR(
    modules=modules,
    optimizer=params.optimizer,
    first_inputs=[first_x, first_y],
)

# Load latest checkpoint to resume training
params.checkpointer.recover_if_possible()
asr_brain.fit(params.epoch_counter, train_set, valid_set)

# Load best checkpoint for evaluation
params.checkpointer.recover_if_possible(lambda c: -c.meta["PER"])
test_stats = asr_brain.evaluate(params.test_loader())
params.train_logger.log_stats(
    stats_meta={"Epoch loaded": params.epoch_counter.current},
    test_stats=test_stats,
)

# Write alignments to file
per_summary = edit_distance.wer_summary(test_stats["PER"])
with open(params.wer_file, "w") as fo:
    wer_io.print_wer_summary(per_summary, fo)
    wer_io.print_alignments(test_stats["PER"], fo)
