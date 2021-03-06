"""
T5 model for question answering
"""

import torch
import argparse
import os
import numpy as np

from typing import Callable, Dict, Iterable, List

from torch import optim
from torch.utils.data import DataLoader

from pytorch_lightning import _logger as log
from pytorch_lightning.core import LightningModule

from transformers import T5Config, T5Tokenizer, T5ForConditionalGeneration

from dataset import QaDataset

from rouge_score import rouge_scorer, scoring


ROUGE_KEYS = ["rouge1", "rouge2", "rougeL"]


def lmap(f: Callable, x: Iterable) -> List:
    """list(map(f, x))"""
    return list(map(f, x))


class T5QaModel(LightningModule):
    """
    T5 model for question answering
    """

    def __init__(self,
                 hparams: argparse.Namespace,
                 num_labels=None,
                 **config_kwargs
                 ) -> 'T5QaModel':
        super().__init__()
        self.hparams = hparams
        cache_dir = self.hparams.cache_dir if self.hparams.cache_dir else None
        self.config = T5Config.from_pretrained(
            self.hparams.config_name if self.hparams.config_name else self.hparams.model_name_or_path,
            **({"num_labels": num_labels} if num_labels is not None else {}),
            cache_dir=cache_dir,
            **config_kwargs,
        )
        self.tokenizer = T5Tokenizer.from_pretrained(
            self.hparams.tokenizer_name if self.hparams.tokenizer_name else self.hparams.model_name_or_path,
            cache_dir=cache_dir,
        )
        self.model = T5ForConditionalGeneration.from_pretrained(
            self.hparams.model_name_or_path,
            from_tf=bool(".ckpt" in self.hparams.model_name_or_path),
            config=self.config,
            cache_dir=cache_dir,
        )

        # fix for eos token id problem
        # see https://github.com/huggingface/transformers/issues/5142 for more info on the problem and workaround
        if self.tokenizer.eos_token_id == 1:
            self.tokenizer.add_special_tokens({'eos_token': '[EOS]'})
            self.model.resize_token_embeddings(len(self.tokenizer))

        self.dataset_kwargs: dict = dict(
            data_dir=self.hparams.input_dir,
            max_source_length=1024,
            max_target_length=56,
        )

        self.loss_names = ["loss"]
        self.metric_names = ROUGE_KEYS
        self.val_metric = "rouge2"

    def forward(self, input_ids, attention_mask=None, decoder_input_ids=None, lm_labels=None):
        """
        No special modification required for Lightning, define it as you normally would
        in the `nn.Module` in vanilla PyTorch.
        """
        return self.model(
            input_ids, attention_mask=attention_mask, decoder_input_ids=decoder_input_ids, lm_labels=lm_labels,
        )

    def ids_to_clean_text(self, generated_ids: List[int]):
        gen_text = self.tokenizer.batch_decode(
            generated_ids, skip_special_tokens=True, clean_up_tokenization_spaces=True
        )
        return lmap(str.strip, gen_text)

    def _step(self, batch):
        pad_token_id = self.tokenizer.pad_token_id
        source_ids, source_mask, y = batch["source_ids"], batch["source_mask"], batch["target_ids"]
        y_ids = y[:, :-1].contiguous()
        lm_labels = y[:, 1:].clone()
        lm_labels[y[:, 1:] == pad_token_id] = -100
        outputs = self(source_ids, attention_mask=source_mask, decoder_input_ids=y_ids, lm_labels=lm_labels,)

        loss = outputs[0]

        return (loss,)

    def _generative_step(self, batch: dict) -> dict:
        pad_token_id = self.tokenizer.pad_token_id
        source_ids, source_mask, y = QaDataset.trim_seq2seq_batch(batch, pad_token_id)
        generated_ids = self.model.generate(input_ids=source_ids, attention_mask=source_mask, use_cache=True, )
        preds = self.ids_to_clean_text(generated_ids)
        target = self.ids_to_clean_text(y)
        loss_tensors = self._step(batch)
        base_metrics = {name: loss for name, loss in zip(self.loss_names, loss_tensors)}
        rouge: Dict = T5QaModel.calculate_rouge(preds, target)
        summ_len = np.mean(lmap(len, generated_ids))
        base_metrics.update(summ_len=summ_len, preds=preds, target=target, **rouge)
        return base_metrics

    def training_step(self, batch, batch_idx):
        """
        Lightning calls this inside the training loop with the data from the training dataloader
        passed in as `batch`.
        """
        loss_tensors = self._step(batch)
        logs = {name: loss for name, loss in zip(self.loss_names, loss_tensors)}
        return {"loss": loss_tensors[0], "log": logs}

    def validation_step(self, batch, batch_idx):
        """
        Lightning calls this inside the validation loop with the data from the validation dataloader
        passed in as `batch`.
        """
        return self._generative_step(batch)

    def validation_end(self, outputs):
        avg_loss = torch.stack([x["loss"] for x in outputs]).mean()
        tensorboard_logs = {"loss": avg_loss}
        return {"avg_loss": avg_loss, "log": tensorboard_logs}

    def test_step(self, batch, batch_idx):
        pad_token_id = self.tokenizer.pad_token_id
        source_ids, source_mask, y = QaDataset.trim_seq2seq_batch(batch, pad_token_id)
        # NOTE: the following kwargs get more speed and lower quality summaries than those in evaluate_cnn.py
        generated_ids = self.model.generate(
            input_ids=source_ids,
            attention_mask=source_mask,
            num_beams=1,
            max_length=80,
            repetition_penalty=2.5,
            length_penalty=1.0,
            early_stopping=True,
            use_cache=True,
        )
        preds = [
            self.tokenizer.decode(g, skip_special_tokens=True, clean_up_tokenization_spaces=True)
            for g in generated_ids
        ]
        target = [self.tokenizer.decode(t, skip_special_tokens=True, clean_up_tokenization_spaces=True) for t in y]
        loss = self._step(batch)

        return {"val_loss": loss, "preds": preds, "target": target}

    def validation_epoch_end(self, outputs):
        """
        Called at the end of validation to aggregate outputs.
        :param outputs: list of individual outputs of each validation step.
        """
        avg_loss = torch.stack([x['val_loss'] for x in outputs]).mean()
        val_acc = sum([x['n_correct_pred'] for x in outputs]) / sum(x['n_pred'] for x in outputs)
        tensorboard_logs = {'val_loss': avg_loss, 'val_acc': val_acc}
        return {'val_loss': avg_loss, 'log': tensorboard_logs}

    def test_epoch_end(self, outputs):
        avg_loss = torch.stack([x['test_loss'] for x in outputs]).mean()
        test_acc = sum([x['n_correct_pred'] for x in outputs]) / sum(x['n_pred'] for x in outputs)
        tensorboard_logs = {'test_loss': avg_loss, 'test_acc': test_acc}
        return {'test_loss': avg_loss, 'log': tensorboard_logs}

    def get_dataloader(self, type_path: str, batch_size: int, shuffle: bool = False) -> DataLoader:
        dataset = QaDataset(self.tokenizer, type_path=type_path, **self.dataset_kwargs)
        num_workers = 2
        if os.name == 'nt':
            # there are problems with parallelizing DataLoader on Windows
            # see https://github.com/pytorch/pytorch/issues/12831
            num_workers = 0

        dataloader = DataLoader(dataset, batch_size=batch_size, collate_fn=dataset.collate_fn, shuffle=shuffle,
                                num_workers=num_workers, pin_memory=True)
        return dataloader

    def configure_optimizers(self):
        """
        Return whatever optimizers and learning rate schedulers you want here.
        At least one optimizer is required.
        """
        optimizer = optim.Adam(self.parameters(), lr=self.hparams.learning_rate)
        scheduler = optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=10)
        return [optimizer], [scheduler]

    def train_dataloader(self) -> DataLoader:
        log.info('Training data loader called.')
        return self.get_dataloader("train", batch_size=self.hparams.train_batch_size, shuffle=True)

    def val_dataloader(self) -> DataLoader:
        log.info('Validation data loader called.')
        return self.get_dataloader("validation", batch_size=self.hparams.eval_batch_size)

    def test_dataloader(self) -> DataLoader:
        log.info('Test data loader called.')
        return self.get_dataloader("test", batch_size=self.hparams.eval_batch_size)

    @staticmethod
    def calculate_rouge(output_lns: List[str], reference_lns: List[str]) -> Dict:
        scorer = rouge_scorer.RougeScorer(ROUGE_KEYS, use_stemmer=True)
        aggregator = scoring.BootstrapAggregator()

        for reference_ln, output_ln in zip(reference_lns, output_lns):
            scores = scorer.score(reference_ln, output_ln)
            aggregator.add_scores(scores)

        result = aggregator.aggregate()
        return {k: v.mid.fmeasure for k, v in result.items()}

    @staticmethod
    def add_model_specific_args(parser):  # pragma: no-cover
        """
        Define parameters that only apply to this model
        """
        # Model specification
        parser.add_argument(
            "--model_name_or_path",
            default=None,
            type=str,
            required=True,
            help="path to pretrained model or model identifier from huggingface.co/models",
        )
        parser.add_argument(
            "--config_name",
            default="",
            type=str,
            help="pretrained config name or path if not the same as model_name"
        )
        parser.add_argument(
            "--tokenizer_name",
            default="",
            type=str,
            help="pretrained tokenizer name or path if not the same as model_name",
        )

        # Cache settings
        parser.add_argument(
            "--cache_dir",
            default="",
            type=str,
            help="where to store the pre-trained models downloaded from s3",
        )

        # Optimizer settings
        parser.add_argument(
            "--max_grad_norm",
            default=1.0,
            type=float,
            help="Max gradient norm.")
        parser.add_argument(
            "--gradient_accumulation_steps",
            type=int,
            default=1,
            help="number of update steps to accumulate before performing a backward/update pass",
        )

        parser.add_argument(
            "--learning_rate",
            default=5e-5,
            type=float,
            help="The initial learning rate for Adam."
        )
        parser.add_argument(
            "--weight_decay",
            default=0.0,
            type=float,
            help="Weight decay if we apply some."
        )
        parser.add_argument(
            "--adam_epsilon",
            default=1e-8,
            type=float,
            help="Epsilon for Adam optimizer."
        )
        parser.add_argument(
            "--epochs",
            default=3,
            type=int,
            help="Total number of training epochs to perform."
        )

        parser.add_argument(
            "--train_batch_size",
            default=8,
            type=int
        )
        parser.add_argument(
            "--eval_batch_size",
            default=8,
            type=int
        )

        return parser
