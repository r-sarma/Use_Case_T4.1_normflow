# Copyright (c) 2021-2024 Javad Komijani

"""
This module contains high-level classes for normalizing flow techniques,
with the central `Model` class integrating essential components such as priors,
networks, and actions. It provides utilities for training and sampling,
along with support for MCMC sampling and device management.
"""

import torch
import time
import os
from pathlib import Path

import numpy as np

from .mcmc import MCMCSampler, BlockedMCMCSampler
from .lib.combo import estimate_logz, fmt_val_err
from .device import ModelDeviceHandler


# =============================================================================
class Model:
    """
    The central high-level class of the package, which integrates instances of
    essential classes (`prior`, `net_`, and `action`) to provide utilities for
    training and sampling. This class interfaces with various core components
    to facilitate training, posterior inference, MCMC sampling, and device
    management.

    Parameters
    ----------
    prior : instance of a `Prior` class
        An instance of a Prior class (e.g., `NormalPrior`) representing the
        model's prior distribution.

    net_ : instance of a `Module_` class
        A model component responsible for the transformations required in the
        model. The trailing underscore indicates that the associated forward
        method computes and returns the Jacobian of the transformation, which
        is crucial in the method of normalizing flows.

    action : instance of an `Action` class
        Defines the model's action, which specified the target distribution
        during training.

    Attributes
    ----------
    fit : Fitter
        An instance of the Fitter class, responsible for training the model.
        `fit` is aliased to `train` for flexibility in usage.

    posterior : Posterior
        An instance of the Posterior class, which manages posterior inference
        on the model parameters.

    mcmc : MCMCSampler
        An instance of the MCMCSampler class, enabling MCMC sampling for
        posterior distributions.

    blocked_mcmc : BlockedMCMCSampler
        An instance of the BlockedMCMCSampler class, providing blockwise
        MCMC sampling for improved sampling efficiency.

    device_handler : ModelDeviceHandler
        Manages the device (CPU/GPU) for model training and inference, ensuring
        seamless operation across hardware setups.
    """

    def __init__(self, *, prior, net_, action):
        self.net_ = net_
        self.prior = prior
        self.action = action

        # Components for training, sampling, and device handling
        self.fit = Fitter(self)
        self.train = self.fit  # Alias for `fit`

        self.posterior = Posterior(self)
        self.mcmc = MCMCSampler(self)
        self.blocked_mcmc = BlockedMCMCSampler(self)
        self.device_handler = ModelDeviceHandler(self)


class Posterior:
    """
    Creates samples directly from a trained probabilistic model.

    The `Posterior` class generates samples from a specified model without
    using an accept-reject step, making it suitable for tasks that require
    quick, direct sampling. All methods in this class use `torch.no_grad()`
    to prevent gradient computation.

    Parameters
    ----------
    model : Model
        A trained model to sample from.

    Methods
    -------
    sample(batch_size=1, **kwargs)
        Returns a specified number of samples from the model.

    sample_(batch_size=1, preprocess_func=None)
        Returns samples and their log probabilities, with an optional
        preprocessing function.

    sample__(batch_size=1, **kwargs)
        Similar to `sample_`, but also returns the log probability of the
        target distribution.

    log_prob(y)
        Computes the log probability of given samples.
    """

    def __init__(self, model: Model):
        self._model = model

    @torch.no_grad()
    def sample(self, batch_size=1, **kwargs):
        """
        Draws samples from the model.

        Parameters
        ----------
        batch_size : int, optional
            Number of samples to draw, default is 1.

        Returns
        -------
        Tensor
            Generated samples.
        """
        return self.sample_(batch_size=batch_size, **kwargs)[0]

    @torch.no_grad()
    def sample_(self, batch_size=1, preprocess_func=None):
        """
        Draws samples and their log probabilities from the model.

        Parameters
        ----------
        batch_size : int, optional
            Number of samples to draw, default is 1.

        preprocess_func : function or None, optional
            A function to adjust the prior samples if needed. It should take
            samples and log probabilities as input and return modified values.

        Returns
        -------
        tuple
            - `y`: Generated samples.
            - `logq`: Log probabilities of the samples.
        """
        x, logr = self._model.prior.sample_(batch_size)

        if preprocess_func is not None:
            x, logr = preprocess_func(x, logr)

        y, logj = self._model.net_(x)
        logq = logr - logj
        return y, logq

    @torch.no_grad()
    def sample__(self, batch_size=1, **kwargs):
        """
        Similar to `sample_`, but also returns the log probability of the
        target distribution from `model.action`.

        Parameters
        ----------
        batch_size : int, optional
            Number of samples to draw, default is 1.

        Returns
        -------
        tuple
            - `y`: Generated samples.
            - `logq`: Log probabilities of the samples.
            - `logp`: Log probabilities from the target distribution.
        """
        y, logq = self.sample_(batch_size=batch_size, **kwargs)
        logp = -self._model.action(y)  # logp is log(p_{non-normalized})
        return y, logq, logp

    @torch.no_grad()
    def log_prob(self, y):
        """
        Computes the log probability of the provided samples.

        Parameters
        ----------
        y : torch.Tensor
            Samples for which to calculate the log probability.

        Returns
        -------
        Tensor
            Log probabilities of the samples.
        """
        x, minus_logj = self._model.net_.reverse(y)
        logr = self._model.prior.log_prob(x)
        logq = logr + minus_logj
        return logq


# =============================================================================
class Fitter:
    """A class for training a given model."""

    def __init__(self, model: Model):
        self._model = model

        self.train_batch_size = 1

        self.train_history = dict(
                loss=[], logqp=[], logz=[], ess=[], rho=[], accept_rate=[]
                )

        self.hyperparam = dict(lr=0.001, weight_decay=0.01)

        self.checkpoint_dict = dict(
            display=False,
            print_stride=10,
            print_batch_size=1024,
            snapshot_path=None,
            epochs_run=0
            )

    def __call__(self,
            n_epochs=1000,
            save_every=None,
            batch_size=64,
            optimizer_class=torch.optim.AdamW,
            scheduler=None,
            loss_fn=None,
            hyperparam={},
            checkpoint_dict={}
            ):

        """Fit the model; i.e. train the model.

        Parameters
        ----------
        n_epochs : int
            Number of epochs of training.

        save_every: int
            save a model every <save_every> epochs.

        batch_size : int
            Size of samples used at each epoch.

        optimizer_class : optimization class, optional
            By default is set to torch.optim.AdamW, but can be changed.

        scheduler : scheduler class, optional
            By default no scheduler is used.

        loss_fn : None or function, optional
            The default value is None, which translates to using KL divergence.

        hyperparam : dict, optional
            Can be used to set hyperparameters like the learning rate and decay
            weights.

        checkpoint_dict : dict, optional
            Can be set to control the printing and saving of the training status.
        """
        self.hyperparam.update(hyperparam)
        self.checkpoint_dict.update(checkpoint_dict)

        snapshot_path = self.checkpoint_dict['snapshot_path']

        if save_every is None:
            save_every = n_epochs

        # decide whether to load a snapshot
        if (snapshot_path is not None) and os.path.exists(snapshot_path):
            print(f"Trying to load snapshot from {snapshot_path}")
            self._load_snapshot()

        self.loss_fn = Fitter.calc_kl_mean if loss_fn is None else loss_fn

        net_ = self._model.net_
        if '_groups' in net_.__dict__.keys():
            parameters = net_.grouped_parameters()
        else:
            parameters = net_.parameters()
        self.optimizer = optimizer_class(parameters, **self.hyperparam)

        if scheduler is None:
            self.scheduler = None
        else:
            self.scheduler = scheduler(self.optimizer)

        if n_epochs > 0:
            self._train(n_epochs, batch_size, save_every)

    def _load_snapshot(self):
        snapshot_path = self.checkpoint_dict['snapshot_path']
        if torch.cuda.is_available():
            gpu_id = self._model.device_handler.rank
            # gpu_id = int(os.environ["LOCAL_RANK"]) might be needed for torchrun ??
            loc = f"cuda:{gpu_id}"
            print(f"GPU: Attempting to load saved model into {loc}")
        else: 
            loc = None  # cpu training
            print("CPU: Attempting to load saved model")
        snapshot = torch.load(snapshot_path, map_location=loc)
        self._model.net_.load_state_dict(snapshot["MODEL_STATE"]) 
        self.checkpoint_dict['epochs_run'] = snapshot['EPOCHS_RUN']
        print(f"Snapshot found: {snapshot_path}\nResuming training via Saved Snapshot at Epoch {snapshot['EPOCHS_RUN']}")

    def _save_snapshot(self, epoch):
        """Save snapshot of training for analysis and/or to continue training
        at a later date.
        """
        snapshot_path = self.checkpoint_dict['snapshot_path']
        epochs_run = epoch + self.checkpoint_dict['epochs_run']
        snapshot_new_path = snapshot_path.rsplit('.',2)[0] + ".E" + str(epochs_run) + ".tar" 
        snapshot = {
                    "MODEL_STATE": self._model.net_.state_dict(),
                     "EPOCHS_RUN": epochs_run }
        torch.save(snapshot, snapshot_new_path)
        print(f"Epoch {epochs_run} | Model Snapshot saved at {snapshot_new_path}")

    def _train(self, n_epochs: int, batch_size: int, save_every: int):

        T1 = time.time()
        for epoch in range(1, n_epochs+1):
            loss, logqp = self.step(batch_size)
            self.checkpoint(epoch, loss, save_every)
            if self.scheduler is not None:
                self.scheduler.step()
        T2 = time.time()
        if n_epochs > 0 and self._model.device_handler.rank == 0:
            print(f"({loss.device}) Time = {T2 - T1:.3g} sec.")

    def step(self, batch_size):
        """Perform a train step with a batch of inputs"""
        net_ = self._model.net_
        prior = self._model.prior
        action = self._model.action

        x, logr = prior.sample_(batch_size)
        y, logJ = net_(x)
        logq = logr - logJ
        logp = -action(y)
        loss = self.loss_fn(logq, logp)

        self.optimizer.zero_grad()  # clears old gradients from last steps

        loss.backward()

        self.optimizer.step()

        return loss, logq - logp

    def checkpoint(self, epoch, loss, save_every):

        rank = self._model.device_handler.rank
        print_stride = self.checkpoint_dict['print_stride']
        print_batch_size = self.checkpoint_dict['print_batch_size']
        snapshot_path = self.checkpoint_dict['snapshot_path']

        # Always save loss on rank 0
        if rank == 0:
            self.train_history['loss'].append(loss.item())
            # Save model as well
            if snapshot_path is not None and (epoch % save_every == 0):
                self._save_snapshot(epoch)

        print_batch_size = print_batch_size // self._model.device_handler.nranks

        if epoch == 1 or (epoch % print_stride == 0):

            _, logq, logp = self._model.posterior.sample__(print_batch_size)

            logq = self._model.device_handler.all_gather_into_tensor(logq)
            logp = self._model.device_handler.all_gather_into_tensor(logp)

            if rank == 0:
                loss_ = self.loss_fn(logq, logp)
                self._append_to_train_history(logq, logp)
                self.print_fit_status(epoch, loss=loss_)

    @staticmethod
    def calc_kl_mean(logq, logp):
        """Return Kullback-Leibler divergence estimated from logq and logp."""
        return (logq - logp).mean()  # KL, assuming samples from q

    @staticmethod
    def calc_kl_var(logq, logp):
        return (logq - logp).var()

    @staticmethod
    def calc_corrcoef(logq, logp):
        return torch.corrcoef(torch.stack([logq, logp]))[0, 1]

    @staticmethod
    def calc_direct_kl_mean(logq, logp):
        logpq = logp - logq
        logz = torch.logsumexp(logpq, dim=0) - np.log(logp.shape[0])
        logpq = logpq - logz  # p is now normalized
        p_by_q = torch.exp(logpq)
        return (p_by_q * logpq).mean()

    @staticmethod
    def calc_minus_logz(logq, logp):
        logz = torch.logsumexp(logp - logq, dim=0) - np.log(logp.shape[0])
        return -logz

    @staticmethod
    def calc_ess(logq, logp):
        """Rerturn effective sample size (ESS)."""
        logqp = logq - logp
        log_ess = 2*torch.logsumexp(-logqp, dim=0) \
                - torch.logsumexp(-2*logqp, dim=0)
        ess = torch.exp(log_ess) / len(logqp)  # normalized
        return ess

    @staticmethod
    def calc_minus_logess(logq, logp):
        """Return logarithm of inverse of effective sample size."""
        logqp = logq - logp
        log_ess = 2*torch.logsumexp(-logqp, dim=0) \
                - torch.logsumexp(-2*logqp, dim=0)
        return - log_ess + np.log(len(logqp))  # normalized

    @torch.no_grad()
    def _append_to_train_history(self, logq, logp):
        logqp = logq - logp
        logz = estimate_logz(logqp, method='jackknife')  # returns (mean, std)
        accept_rate = self._model.mcmc.estimate_accept_rate(logqp)
        ess = self.calc_ess(logqp, 0)
        rho = self.calc_corrcoef(logq, logp)
        logqp = (logqp.mean().item(), logqp.std().item())
        self.train_history['logqp'].append(logqp)
        self.train_history['logz'].append(logz)
        self.train_history['ess'].append(ess)
        self.train_history['rho'].append(rho)
        self.train_history['accept_rate'].append(accept_rate)

    def print_fit_status(self, epoch, loss=None):
        mydict = self.train_history
        if loss is None:
            loss = mydict['loss'][-1]
        else:
            pass  # the printed loss can be different from mydict['loss'][-1]
        logqp_mean, logqp_std = mydict['logqp'][-1]
        logz_mean, logz_std = mydict['logz'][-1]
        accept_rate_mean, accept_rate_std = mydict['accept_rate'][-1]
        # We now incorporate the effect of estimated log(z) to mean of log(q/p)
        adjusted_logqp_mean = logqp_mean + logz_mean
        ess = mydict['ess'][-1]
        rho = mydict['rho'][-1]

        if epoch == 1:
            print(f"\n>>> Training progress ({ess.device}) <<<\n")
            print("Note: log(q/p) is estimated with normalized p; " \
                  + "mean & error are obtained from samples in a batch\n")

        epoch += self.checkpoint_dict['epochs_run']
        str1 = f"Epoch: {epoch} | loss: {loss:.4f} | ess: {ess:.4f}"
        print(str1)


# =============================================================================
@torch.no_grad()
def reverse_flow_sanitychecker(model, n_samples=4, net_=None):
    """Performs a sanity check on the reverse method of modules."""

    if net_ is None:
        net_ = model.net_

    x = model.prior.sample(n_samples)
    y, logj = net_(x)
    x_hat, minus_logj = net_.backward(y)

    mean = lambda z: z.abs().mean().item()

    print("reverse method is OK if following values vanish (up to round off):")
    print(f"{mean(x - x_hat):g} & {mean(1 + minus_logj / logj):g}")
