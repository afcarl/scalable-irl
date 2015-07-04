"""
Sampling (MCMC) based approximate inference for TBIRL
"""

from __future__ import division

from abc import ABCMeta, abstractmethod
from copy import deepcopy
from random import randrange

from numpy.random import choice, randint, uniform
from scipy.misc import logsumexp
import numpy as np

from ..base import ModelMixin
from .graph_birl import TBIRL


########################################################################
# MCMC proposals

class Proposal(ModelMixin):
    """ Proposal for MCMC sampling """
    __meta__ = ABCMeta

    def __init__(self, dim):
        self.dim = dim

    @abstractmethod
    def __call__(self, loc):
        raise NotImplementedError('Abstract class')


class PolicyWalkProposal(Proposal):
    """ PolicyWalk MCMC proposal """
    def __init__(self, dim, delta, bounded=True):
        super(PolicyWalkProposal, self).__init__(dim)
        self.delta = delta
        self.bounded = bounded
        # TODO - allow setting bounds as list of arrays

    def __call__(self, loc):
        new_loc = np.array(loc)
        changed = False
        while not changed:
            d = choice([-self.delta, self.delta])
            i = randint(self.dim)
            if self.bounded:
                if -1 <= new_loc[i]+d <= 1:
                    new_loc[i] += d
                    changed = True
            else:
                new_loc[i] += d
                changed = True
        return new_loc


########################################################################


class TBIRLPolicyWalk(TBIRL):
    """GraphBIRL algorithm using PolicyWalk MCMC

    Bayesian Inverse Reinforcement Learning on Adaptive State-Graphs using
    PolicyWalk (TBIRL-PW)

    Reward posterior disctribution is computed using MCMC samples via a
    grid walk on the space of rewards.

    Parameters
    ----------
    demos : array-like, shape (M x d)
        Expert demonstrations as M trajectories of state action pairs
    mdp : ``GraphMDP`` object
        The underlying (semi) Markov decision problem
    prior : ``RewardPrior`` object
        Reward prior callable
    loss : callable
        Reward loss callable
    step_size : float, optional (default=0.2)
        Grid walk step size
    burn : float, optional (default=0.2)
        Fraction of MCMC samples to throw away before the chain stabilizes
    max_iter : int, optional (default=10)
        Number of iterations of the TBIRL algorithm
    beta : float, optional (default=0.9)
        Expert optimality parameter for softmax Boltzman temperature
    reward_max : float, optional (default=1.0)
        Maximum value of the reward signal (for a single dimension)
    mcmc_iter : int, optional (default=200)
        Number of MCMC samples to use in the PolicyWalk algorithm

    Attributes
    -----------
    _delta : float, optional (default=0.2)
        Grid walk step size
    _rmax : float, optional (default=1.0)
        Maximum value of the reward signal (for a single dimension)
    _mcmc_iter : int, optional (default=200)
        Number of MCMC samples to use in the PolicyWalk algorithm
    _burn : float, optional (default=0.2)
        Fraction of MCMC samples to throw away before the chain stabilizes

    Note
    -----
    Using small step sizes generally implies the need for more samples

    """
    def __init__(self, demos, mdp, prior, loss, step_size=0.3, burn=0.2,
                 max_iter=10, beta=0.9, reward_max=1.0, mcmc_iter=200,
                 cooling=False):
        super(TBIRLPolicyWalk, self).__init__(demos, mdp, prior, loss,
                                              beta, max_iter)
        self._delta = step_size
        self._rmax = reward_max
        self._mcmc_iter = mcmc_iter
        self._burn = burn
        self._tempered = cooling

        # some data for diagnosis
        self.data = dict()
        self.data['qloss'] = []
        self.data['trace'] = []
        self.data['walk'] = []
        self.data['accept_ratios'] = []
        self.data['iter_rewards'] = []

    def initialize_reward(self):
        """
        Generate initial reward for the algorithm in $R^{|S| / \delta}$
        """
        rdim = self._mdp._reward.dim
        loc = [-self._rmax + i * self._delta
               for i in range(int(self._rmax / self._delta + 1))]
        r = [loc[randrange(int(self._rmax / self._delta + 1))]
             for _ in range(rdim)]
        reward = np.array(r)
        if self._tempered:
            # initialize to the maximum of prior
            prior = self._prior(reward)
            reward = np.array([max(prior) for _ in range(rdim)])

        return reward

    def find_next_reward(self, g_trajs):
        """ Compute a new reward based on current generated trajectories """
        return self._policy_walk(g_trajs)

    # -------------------------------------------------------------
    # internals
    # -------------------------------------------------------------

    def _policy_walk(self, g_trajs):
        """ Policy Walk MCMC reward posterior computation """
        r = self.initialize_reward()
        r_mean = deepcopy(r)
        p_dist = PolicyWalkProposal(r.shape[0], self._delta, bounded=True)

        QE = self._expert_trajectory_quality(r)
        QPi = self._generated_trajectory_quality(r, g_trajs)
        ql = sum([sum(Qe - Qp for Qe, Qp in zip(QE, Q_i)) for Q_i in QPi])
        self.data['qloss'].append(ql)
        burn_point = int(self._mcmc_iter * self._burn / 100)

        for step in range(1, self._mcmc_iter+1):
            r_new = p_dist(loc=r_mean)
            QE_new = self._expert_trajectory_quality(r_new)
            QPi_new = self._generated_trajectory_quality(r_new, g_trajs)

            mh_ratio = self._mh_ratio(r_mean, r_new, QE, QE_new, QPi, QPi_new)
            accept_probability = min(1, mh_ratio)
            if self._tempered:
                accept_probability = min(1, mh_ratio) ** self._cooling(step)

            if accept_probability > uniform(0, 1):
                r_mean = self._iterative_reward_mean(r_mean, r_new, step)
                self.data['accept_ratios'].append(1)

            # - handling sample burning
            if step > burn_point:
                self.data['trace'].append(r_mean)
                self.data['walk'].append(r_new)

            if step % 10 == 0:
                print('It: %s, R: %s, R_mean: %s' % (step, r_new, r_mean))
            # self.debug('It: %s, R: %s, R_mean: %s' % (step, r_new, r_mean))

        self.data['iter_rewards'].append(r_mean)
        return r_mean

    def _mh_ratio(self, r, r_new, QE, QE_new, QPi, QPi_new):
        """ Compute the Metropolis-Hastings acceptance ratio

        Given a new reward (weights), MH ratio is used to determine whether or
        not to accept the reward sample.

        Parameters
        -----------
        r : array-like, shape (reward-dim)
            Current reward
        r_new : array-like, shape (reward-dim)
            New reward sample from the MCMC walk
        QE : array-like
            Quality of the expert trajectories based on reward ``r``
        QE_new : array-like
            Quality of the expert trajectories based on reward ``r_new``
        QPi : array-like
            Quality of the generated trajectories based on reward ``r``
        QPi_new : array-like
            Quality of the generated trajectories based on reward ``r_new``

        Returns
        --------
        mh_ratio : float
            The ratio corresponding to :math:`P(r_n|O) / P(r|O) x P(r_n)/P(r)`
        """
        # - initialize reward posterior distribution to log priors
        p_new = np.sum(self._prior.log_p(r_new))
        p = np.sum(self._prior.log_p(r))

        # - log-likelihoods
        z = []
        for q_e in QE:
            for QP_i in QPi:
                for q_i in QP_i:
                    z.append(self._beta*(q_i - q_e))
        lk = -logsumexp(z)

        z_new = []
        for q_e_new in QE_new:
            for QP_i_new in QPi_new:
                for q_i_new in QP_i_new:
                    z_new.append(self._beta*(q_i_new - q_e_new))
        lk_new = -logsumexp(z_new)

        mh_ratio = (lk_new + p_new) / (lk + p)
        return mh_ratio

    def _iterative_reward_mean(self, r_mean, r_new, step):
        """ Iterative mean reward

        Compute the iterative mean of the reward using the running mean
        and a new reward sample
        """
        r_mean = [((step - 1) / float(step)) * m_r + 1.0 / step * r
                  for m_r, r in zip(r_mean, r_new)]
        return np.array(r_mean)

    def _cooling(self, step):
        """ Tempering """
        return 5 + step / 50.0
