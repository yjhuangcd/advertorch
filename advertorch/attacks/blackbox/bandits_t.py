# Copyright (c) 2018-present, Royal Bank of Canada.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
#

from typing import Optional

import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F

from advertorch.attacks.base import Attack
from advertorch.attacks.base import LabelMixin

from advertorch.attacks.blackbox.utils import _check_param, _flatten

def bandit_attack(
        x, loss_fn, eps, clip_min, clip_max, order='l2',
        delta_init=None, prior_init=None, fd_eta=0.01, exploration=0.01, 
        online_lr=0.1, nb_iter=40, eps_iter=0.01
    ):
    """
    Performs the BanditAttack
    Paper: https://arxiv.org/pdf/1807.07978.pdf

    :param x: input data.
    :param loss_fn: loss function.
    :param eps: maximum distortion.
    :param order: (optional) the order of maximum distortion ('linf' or 'l2').
    :param clip_min: mininum value per input dimension (default 0.)
    :param clip_max: mininum value per input dimension (default 1.)
    :param delta_init: (default None)
    :param prior_init: (default None)
    :param fd_eta: step-size used for finite difference grad estimate (default 0.01)
    :param exploration: scales the exploration around prior (default 0.01)
    :param online_lr: learning rate for the prior (default 0.1)
    :param nb_iter: number of iterations (default 40)
    :param eps_iter: attack step size (default 0.01)

    :return: tuple of tensors containing (1) the adv example, (2) the prior
    """
    #if so, we could just use the same estimate grad
    ndim = np.prod(list(x.shape[1:]))
    #The idea of this is that the gradient becomes more accurate as we
    #call it multiple times.
    #This gradient is learnt in an online fashion.

    if delta_init is None:
        adv = x.clone()
    else:
        adv = x + delta_init

    if prior_init is None:
        prior = torch.zeros_like(x)
    else:
        prior = prior_init.clone()

    for t in range(nb_iter):
        #before: # [nbatch, ndim, nsamples]
        #now: # [nbatch, ndim]
        exp_noise = exploration * torch.randn_like(prior)/(ndim**0.5)
        
        # Query deltas for finite difference estimator
        ##...this step needs to change
        q1 = F.normalize(prior + exp_noise, dim=-1)
        q2 = F.normalize(prior - exp_noise, dim=-1)
        # Loss points for finite difference estimator
        L1 = loss_fn(adv + fd_eta * q1) # L(prior + c*noise)
        L2 = loss_fn(adv + fd_eta * q2) # L(prior - c*noise)

        delta_L = (L1 - L2)/(fd_eta * exploration) #[nbatch]
        
        grad_est = delta_L * exp_noise
        if order == 'l2':
            #update prior
            prior = prior + online_lr * grad_est
            #make step with prior
            #note the (+): this indicates gradient ascent on the loss
            adv = adv + eps_iter * F.normalize(prior, dim=-1)
            #project
            delta = adv - x
            norms = torch.sqrt( (delta ** 2).sum(-1))
            out_of_bounds_mask = (norms > eps).float().unsqueeze(-1)
            #if out_of_bounds, use the clipped values
            clipped = (x + eps[:, None] * F.normalize(delta, dim=-1))
            adv = clipped * out_of_bounds_mask + adv * (1 - out_of_bounds_mask)
        elif order == 'linf':
            #update prior (exponentiated gradients)
            prior = (prior + 1) / 2 # from [-1, 1] to [0, 1]
            pos = prior * torch.exp(online_lr * grad_est)
            neg = (1 - prior) * torch.exp(-online_lr * grad_est)
            prior = 2 * pos / (pos + neg) - 1
            #make step with prior
            adv = adv + eps_iter * torch.sign(prior)
            #project
            delta = torch.minimum(adv - x, eps[:, None])
            delta = torch.maximum(delta, -eps[:, None])
            adv = x + delta
        else:
            error = "Only order = 'linf', order = 'l2' have been implemented"
            raise NotImplementedError(error)
        
        adv = torch.maximum(adv, clip_min)
        adv = torch.minimum(adv, clip_max)

    return adv, prior

class BanditAttack(Attack, LabelMixin):
    """
    Implementation of "Prior Convictions"
    Paper: https://arxiv.org/pdf/1807.07978.pdf
    Original implementation here: https://github.com/MadryLab/blackbox-bandits
    
    :param predict: forward pass function.
    :param eps: maximum distortion.
    :param order: the order of maximum distortion ('linf' or 'l2', default l2)
    :param fd_eta: step-size used for finite difference grad estimate (default 0.01)
    :param exploration: scales the exploration around prior (default 0.01)
    :param online_lr: learning rate for the prior (default 0.1)
    :param loss_fn: loss function, defaults to CrossEntropyLoss
    :param nb_iter: number of iterations (default 40)
    :param eps_iter: attack step size (default 0.01)
    :param clip_min: mininum value per input dimension (default 0.)
    :param clip_max: mininum value per input dimension (default 1.)
    :param targeted:bool: if the attack is targeted (default False)
    """
    def __init__(
            self, predict, eps: float, order,
            fd_eta=0.01, exploration=0.01, online_lr=0.1,
            loss_fn=None, 
            nb_iter=40,
            eps_iter=0.01,
            clip_min=0., clip_max=1.,
            targeted : bool = False
            ):
        super().__init__(predict, loss_fn, clip_min, clip_max)

        self.eps = eps
        self.order = order
        self.fd_eta = fd_eta
        self.exploration = exploration
        self.online_lr = online_lr
        self.targeted = targeted
        if self.loss_fn is None:
            self.loss_fn = nn.CrossEntropyLoss(reduction="sum")

        self.nb_iter = nb_iter
        self.eps_iter = eps_iter

    def perturb(  # type: ignore
        self,
        x: torch.FloatTensor,
        y: Optional[torch.Tensor]=None
    ) -> torch.FloatTensor:
        """
        Given examples (x, y), returns their adversarial counterparts with
        an attack length of eps.

        :param x: input tensor.
        :param y: label tensor.
                  - if None and self.targeted=False, compute y as predicted
                    labels.
                  - if self.targeted=True, then y must be the targeted labels.
        :return: tensor containing perturbed inputs.
        """
        x, y = self._verify_and_process_inputs(x, y)
        shape, flat_x = _flatten(x)
        
        eps = _check_param(self.eps, x.new_full((x.shape[0],), 1), 'eps')
        clip_min = _check_param(self.clip_min, flat_x, 'clip_min')
        clip_max = _check_param(self.clip_max, flat_x, 'clip_max')

        scale = -1 if self.targeted else 1
        def L(x): #loss func
            input = x.reshape(shape)
            output = self.predict(input)
            loss = scale * self.loss_fn(output, y)
            return loss

        adv, _ = bandit_attack(
            flat_x, loss_fn=L, eps=eps, order=self.order, clip_min=clip_min,
            clip_max=clip_max, delta_init=None, prior_init=None,
            fd_eta=self.fd_eta, exploration=self.exploration, 
            online_lr=self.online_lr, nb_iter=self.nb_iter, 
            eps_iter=self.eps_iter
        )

        adv = adv.reshape(shape)
        
        return adv
        


