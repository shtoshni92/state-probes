import sys
import os
import time
import logging
import random

from os import path

import torch
from transformers import BartTokenizerFast
from transformers import BartForConditionalGeneration
import numpy as np

from data.alchemy.parseScone import loadData
from data_transformer import convert_to_transformer_batches


logging.basicConfig(format='%(asctime)s - %(message)s', level=logging.INFO)
logger = logging.getLogger()


class Experiment(object):

	def __init__(self, args):
		super().__init__()
		self.args = args

		self.device = 'cpu'
		if torch.cuda.is_available():
			self.device = 'cuda'

		# Whether to train or not
		self.eval_model: bool = self.args.eval

		# Step 1 - Build model
		self._build_model()

		# Step 2 - Load Data - Data processing choices such as tokenizer will depend on the model
		self._load_data()

		# Step 3 - Load model and resume training if required
		# Firstly initialize dictionary to track key training variables
		self.train_info = {'best_val_loss': 10**10, 'global_steps': 0, 'num_epochs': 0, 'num_stuck_evals': 0}

		if self.eval_model:
			# Load the best checkpoint
			self._load_previous_checkpoint(last_checkpoint=False)
		else:
			# Resume training
			self._build_optimizers()
			# Loading the checkpoint also restores the training metadata
			self._load_previous_checkpoint(last_checkpoint=True)

			# All set to resume training
			# But first check if training is remaining
			if self._is_training_remaining():
				self.train()

		# Step 4 - Perform final evaluation
		if path.exists(self.best_model_path):
			self.load_model(self.best_model_path, last_checkpoint=False)
			self.periodic_model_eval()
		elif path.exists(self.model_path):
			logger.info("Couldn't find the best model! Using the last checkpoint!")
			self.load_model(self.model_path, last_checkpoint=True)
			self.final_eval()
		else:
			logger.info("No model accessible!")
			sys.exit(1)

	def _build_model(self):
		"""Constructs the model with given config."""
		model_fp = 'facebook/bart-base'
		self.tokenizer = BartTokenizerFast.from_pretrained(model_fp)

		self.model = BartForConditionalGeneration.from_pretrained(model_fp)

		if torch.cuda.is_available():
			self.model.cuda()

		if self.args.use_state_loss:
			PROBE_START = '[PROBE_START]'
			PROBE_END = '[PROBE_END]'
			self.tokenizer.add_special_tokens({
				'additional_special_tokens': [PROBE_START, PROBE_END]
			})
			self.model.resize_token_embeddings(len(self.tokenizer))

			self.probing_tokens_mask = [0.0] * (len(self.tokenizer) - 2) + [1.0, 1.0]

	def _load_data(self):
		# loading data
		self.dataset, lang_v, state_v = loadData(
			split="train", kind="alchemy", synthetic=False, base_dir=self.args.base_dir)
		# TODO (Remove this)
		# self.dataset = self.dataset[:2000]
		self.dev_dataset, lang_v_dev, state_v_dev = loadData(
			split="dev", kind="alchemy", synthetic=False, base_dir=self.args.base_dir)
		self.all_train_states = [" ".join(state) for _, _, state in [x for d in self.dataset for x in d.all_pairs()]]
		self.all_dev_states = [" ".join(state) for _, _, state in [x for d in self.dev_dataset for x in d.all_pairs()]]

	def _load_previous_checkpoint(self, last_checkpoint=True):
		"""Loads the last checkpoint or best checkpoint.

		Args:
			last_checkpoint: If true, load the last checkpoint to resume training.
				Otherwise, load the best model for evaluation.
				If the above two models don't exist, set the random seeds for training initialization.
		"""
		self.model_path = self.args.model_path
		self.best_model_path = self.args.best_model_path

		if last_checkpoint:
			# Resume training
			if path.exists(self.model_path):
				self.load_model(self.model_path, last_checkpoint=last_checkpoint)
			else:
				# Starting training
				torch.random.manual_seed(self.args.seed)
				np.random.seed(self.args.seed)
				random.seed(self.args.seed)
		else:
			# Load best model
			if path.exists(self.best_model_path):
				self.load_model(self.best_model_path, last_checkpoint=last_checkpoint)
			else:
				raise IOError(f"Best model path at {self.best_model_path} not found")

			logger.info("\nModel loaded\n")
			sys.stdout.flush()

	def _is_training_remaining(self):
		"""Check if training is done or remaining.

		There are two cases where we don't resume training:
		(a) The dev performance has not improved for the allowed patience parameter.
		(b) Number of gradient updates is already >= Total training steps.

		Returns:
			bool: If true, we resume training. Otherwise do final evaluation.
		"""
		if self.train_info['num_stuck_evals'] >= self.args.patience:
			return False
		if self.train_info['num_epochs'] >= self.args.epochs:
			return False

		return True

	def _build_optimizers(self):
		"""Initialize model + optimizer(s). Check if there's a checkpoint in which case we resume from there."""
		# Optimizer for clustering params
		self.optimizer = torch.optim.AdamW(self.model.parameters(), lr=self.args.lr)

	def train(self) -> None:
		"""Method for training the model.

		This method implements the training loop.
		Within the training loop, the model is periodically evaluated on the dev set(s).
		"""
		model, optimizer = self.model, self.optimizer
		model.train()

		start_time = time.time()
		eval_time = {'total_time': 0, 'num_evals': 0}
		while True:
			logger.info("Steps done %d" % (self.train_info['global_steps']))
			lang_train_losses = []
			# state_losses = []
			model.train()

			for j, (inputs, lang_tgts, state_tgts, raw_state_targets, init_states) in enumerate(
					convert_to_transformer_batches(
						self.dataset, self.tokenizer, self.args.batchsize, random=random,
						domain="alchemy", device=self.device, add_state=self.args.add_state,
					)
			):

				def handle_example(inputs, lang_tgts, state_tgts):
					if self.args.use_state_loss and random.random() <= self.args.rap_prob:
						# print(self.tokenizer.batch_decode(state_tgts['input_ids'])[0])
						return_dict = model(
							input_ids=inputs['input_ids'], attention_mask=inputs['attention_mask'],
							labels=state_tgts['input_ids'], return_dict=True,
						)
					else:
						return_dict = model(
							input_ids=inputs['input_ids'], attention_mask=inputs['attention_mask'],
							labels=lang_tgts['input_ids'], return_dict=True,
						)

					lang_loss: torch.Tensor = return_dict.loss

					self.train_info['global_steps'] += 1
					optimizer.zero_grad()
					lang_loss.backward()

					optimizer.step()
					lang_train_losses.append(lang_loss.item())

					return lang_loss.item()

				lang_loss = handle_example(inputs, lang_tgts, state_tgts)
				if j % 100 == 0:
					output_str = f"epoch {self.train_info['num_epochs']}, batch {j}, lang score: {lang_loss: .3f}"
					logger.info(output_str)

			self.train_info['num_epochs'] += 1
			logger.info(f"epoch {self.train_info['num_epochs']}, avg lang loss {sum(lang_train_losses) / len(lang_train_losses)}")
			dev_loss = self.periodic_model_eval()
			# Get elapsed time
			elapsed_time = time.time() - start_time

			start_time = time.time()
			logger.info(
				"Steps: %d, Log-loss: %.3f, Best log-loss: %.3f, Time: %.2f"
				% (self.train_info['global_steps'], dev_loss, self.train_info['best_val_loss'], elapsed_time))

			# Check stopping criteria
			if not self._is_training_remaining():
				break

	@torch.no_grad()
	def periodic_model_eval(self):
		model = self.model
		model.eval()
		n_val = 0
		tot_val_loss = 0
		for j, (inputs, lang_tgts, state_tgts, raw_state_targets, init_states) in enumerate(
				convert_to_transformer_batches(
					self.dev_dataset, self.tokenizer, self.args.batchsize,
					domain="alchemy", device=self.device, add_state=self.args.add_state,
				)
		):
			return_dict = model(input_ids=inputs['input_ids'], attention_mask=inputs['attention_mask'],
								labels=lang_tgts['input_ids'], return_dict=True)

			if self.args.use_state_loss:
				loss_fct = torch.nn.CrossEntropyLoss()
				lm_logits = return_dict.logits
				lm_logits = lm_logits.view(-1, len(self.tokenizer))

				logit_mask = torch.tensor(self.probing_tokens_mask, dtype=torch.float32, device=inputs['input_ids'].device)
				lm_logits = lm_logits * (1 - logit_mask) + logit_mask * (-1e10)
				lang_loss = loss_fct(lm_logits, lang_tgts['input_ids'].view(-1))
			else:
				lang_loss = return_dict.loss

			tot_val_loss += lang_loss.item() * len(inputs['input_ids'])
			n_val += len(inputs['input_ids'])

		logger.info(f"n_val: {n_val}")
		avg_val_loss = tot_val_loss / n_val
		logger.info(f"epoch {self.train_info['num_epochs']}, avg val loss: {avg_val_loss}")

		if avg_val_loss <= self.train_info['best_val_loss']:
			logger.info("NEW BEST MODEL")
			self.train_info['best_val_loss'] = avg_val_loss
			self.train_info['num_stuck_evals'] = 0
			self.save_model(self.args.best_model_path, last_checkpoint=False)
		else:
			self.train_info['num_stuck_evals'] += 1

		self.save_model(self.args.model_path, last_checkpoint=True)
		return avg_val_loss

	@torch.no_grad()
	def final_eval(self):
		model = self.model
		model.eval()
		n_val = 0
		tot_val_loss = 0
		for j, (inputs, lang_tgts, state_tgts, raw_state_targets, init_states) in enumerate(
				convert_to_transformer_batches(
					self.dev_dataset, self.tokenizer, self.args.batchsize,
					domain="alchemy", device=self.device, add_state=self.args.add_state,
				)
		):
			return_dict = model(input_ids=inputs['input_ids'], attention_mask=inputs['attention_mask'],
								labels=lang_tgts['input_ids'], return_dict=True)

			if self.args.use_state_loss:
				loss_fct = torch.nn.CrossEntropyLoss()
				lm_logits = return_dict.logits
				lm_logits = lm_logits.view(-1, len(self.tokenizer))

				logit_mask = torch.tensor(self.probing_tokens_mask, dtype=torch.float32,
										  device=inputs['input_ids'].device)
				lm_logits = lm_logits * (1 - logit_mask) + logit_mask * (-1e10)
				lang_loss = loss_fct(lm_logits, lang_tgts['input_ids'].view(-1))
			else:
				lang_loss = return_dict.loss

			tot_val_loss += lang_loss.item() * len(inputs['input_ids'])
			n_val += len(inputs['input_ids'])

		logger.info(f"n_val: {n_val}")
		avg_val_loss = tot_val_loss / n_val
		logger.info(f"epoch {self.train_info['num_epochs']}, avg val loss: {avg_val_loss}")
		return avg_val_loss

	def load_model(self, location: str, last_checkpoint=True) -> None:
		"""Load model from given location.

		Args:
			location: str
				Location of checkpoint
			last_checkpoint: bool
				Whether the checkpoint is the last one saved or not.
				If false, don't load optimizers, schedulers, and other training variables.
		"""

		checkpoint = torch.load(location, map_location='cpu')
		self.args = checkpoint['args']
		self.model.load_state_dict(checkpoint['model'], strict=False)

		if last_checkpoint:
			# If resuming training, restore the optimizer state as well
			self.optimizer.load_state_dict(checkpoint['optimizer'])

			self.train_info = checkpoint['train_info']
			torch.set_rng_state(checkpoint['rng_state'])
			np.random.set_state(checkpoint['np_rng_state'])

	def save_model(self, location: os.PathLike, last_checkpoint=True) -> None:
		"""Save model.

		Args:
			location: Location of checkpoint
			last_checkpoint:
				Whether the checkpoint is the last one saved or not.
				If false, don't save optimizers and schedulers which take up a lot of space.
		"""

		save_dict = {
			'train_info': self.train_info,
			'model': self.model.state_dict(),
			'rng_state': torch.get_rng_state(),
			'np_rng_state': np.random.get_state(),
			'args': self.args,
		}

		if last_checkpoint:
			# For last checkpoint save the optimizer and scheduler states as well
			save_dict['optimizer'] = self.optimizer.state_dict()
			save_dict['scheduler'] = {}

		torch.save(save_dict, location)
		logging.info(f"Model saved at: {location}")

