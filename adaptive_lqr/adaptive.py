"""adaptive.py

An object-oriented approach to implementing
different adaptive strategies.

"""


import numpy as np
import adaptive_lqr.utils as utils
import logging
import time
import itertools as it
from scipy.linalg import lu

from abc import ABC, abstractmethod
from planner.SE3_planner import SE3Planner
from utilsStuff.enum_class import TrajType, ControllerType, LiniearizationType
from manifpy import SE2, SE2Tangent, SO2, SO2Tangent
from manifpy import SE3, SE3Tangent, SO3, SO3Tangent
from environments.numerical_simulator.single_rigid_body_simulator import SingleRigidBodySimulator



class AdaptiveMethod(ABC):
    """The base class for all adaptive methods

    The way to use this class is as follows:

    e = MyAdaptiveMethod(...)
    e.reset(rng)
    e.prime(num_iters, static_feedback, rng)
    for _ in range(horizon):
        cur_regret = e.step(rng)
        # do something with current regret
        # ...

    """

    def __init__(self, Q, R, A_star, B_star, sigma_w, rls_lam):
        """

        """

        # TODO: validate inputs

        self._Q = np.array(Q)
        self._R = np.array(R)
        self._A_star = np.array(A_star)
        self._B_star = np.array(B_star)
        self._sigma_w = sigma_w
        self._rls_lam = rls_lam
        self._n, self._p = B_star.shape

        if self._rls_lam is not None:
            self._rls = utils.RecursiveLeastSquaresEstimator(self._n, self._p, self._rls_lam)
        else:
            self._rls = None

        self._P_star, self._K_star = utils.dlqr(self._A_star, self._B_star, self._Q, self._R)
        self._J_star = (self._sigma_w ** 2) * np.trace(self._P_star)

    @abstractmethod
    def _design_controller(self, states, inputs, transitions, rng):
        """Design a controller for the next epoch.

        Called after an epoch is finished. Is handed two matrices
        of the history of states and inputs.

        returns a best estimate (Ahat, Bhat) of the true system.
        """

        pass

    @abstractmethod
    def _get_input(self, state, rng):
        """Obtain the next input to play from the current state

        """

        pass

    def _on_iteration_completion(self):
        """Called after an iteration is complete

        """

        pass

    def _on_epoch_completion(self):
        """Called after an epoch is complete

        """

        pass

    @abstractmethod
    def _should_terminate_epoch(self):
        """Return true if the epoch should terminate

        This method should not mutate state.

        """

        pass

    def reset(self, rng):
        """Reset both the dynamics and internal state.

        Must be called before ether prime() or step() is called.
        """
        self._state_history = []
        self._input_history = []
        self._transition_history = []
        self._cost_history = []

        # tracks the estimate errors for each epoch
        self._error_history = []

        # tracks the length of epochs
        self._epoch_history = []

        # tracks the average infinite time horizon cost
        self._infinite_horizon_cost_history = []

        if self._rls is not None:
            logger = self._get_logger()
            logger.debug("Using RLS estimator with rls_lam={}".format(self._rls_lam))
            self._rls = utils.RecursiveLeastSquaresEstimator(self._n, self._p, self._rls_lam)
        self._rls_history = []

        self._regret = 0
        self._epoch_idx = 0
        self._iteration_idx = 0
        self._iteration_within_epoch_idx = 0
        self._state_cur = np.zeros((self._n,))
        self._last_reset_time = time.time()
        self._has_primed = False
        self.estimated_l = None
        self.estimated_r = None

    def prime(self, num_iterations, static_feedback, excitation, rng, config, lti):
        """Initialize the adaptive method with rollouts

        Must be called after reset() and before step() is called

        """

        assert num_iterations >= 1
        assert excitation > 0

        rng = self._get_rng(rng)
        # config = {'type': TrajType.CONSTANT,
        #           'param': {'start_state': np.array([0, 0, 0, 0, 0, 0, 1]),
        #                     'linear_vel': np.array([2, 0, 0.2]),
        #                     'angular_vel': np.array([0, 0, 1]),
        #                     'dt': lti.dt,
        #                     'nTraj': num_iterations}}
        config['param']['nTraj'] = num_iterations
        planner = SE3Planner(config)
        ref_SE3, ref_twist, dt = planner.generate_constant_traj(config['param'])

        simulator = SingleRigidBodySimulator(dt)
        K = static_feedback

        for i in range(num_iterations-1):
            pos = simulator.curr_state[0:3]
            quat = simulator.curr_state[3:3 + 4]
            curr_SE3 = SE3(pos, quat)
            curr_omega_vel = simulator.curr_state[7:7 + 3 + 3]

            curr_SE3_ref = SE3(ref_SE3[0:3, i], ref_SE3[3:3 + 4, i])
            ref_omega_vel = ref_twist[:, i]
            x = np.zeros(12)
            x_log = curr_SE3_ref.between(curr_SE3).log().coeffs()
            x[0:3] = x_log[3: 3 + 3]
            x[3:6] = x_log[0: 0 + 3]
            x[6:12] = curr_omega_vel - ref_omega_vel
            u = K.dot(x) + excitation * rng.normal(size=(self._p,)) + lti.ud
            next_state = simulator.step(u - lti.ud)
            self._state_history.append(x)
            self._input_history.append(u)
            next_SE3_ref = ref_SE3[:, i+1]
            next_ref_omega_vel = ref_twist[:, i+1]
            xnext = np.zeros(12)
            x_next_log = SE3(next_SE3_ref[0:3], next_SE3_ref[3:3+4]).between(
                SE3(next_state[0:3], next_state[3:3+4])).log().coeffs()
            xnext[0:3] = x_next_log[3: 3 + 3]
            xnext[3:6] = x_next_log[0: 0 + 3]
            xnext[6:12] = next_state[7:7+3+3] - next_ref_omega_vel
            self._transition_history.append(xnext)


            if self._rls is not None:
                self._rls.update(self._state_cur, u, xnext)
                Ahat, Bhat, _ = self._rls.get_estimate()
                eps_A = np.linalg.norm(Ahat - self._A_star, ord=2)
                eps_B = np.linalg.norm(Bhat - self._B_star, ord=2)
                self._rls_history.append((eps_A, eps_B))

            self._state_cur = xnext

        Ahat, Bhat, Jnom = self._design_controller(
                np.array(self._state_history),
                np.array(self._input_history),
                np.array(self._transition_history),
                rng)
        print("Bhat: ", Bhat)
        I_hat_inv = Bhat[6:9, 0:3] / dt
        I_hat = np.linalg.inv(I_hat_inv)
        I_hat = (I_hat + I_hat.T) / 2
        print("I_hat: ", I_hat)
        m_hat_inv = Bhat[9:12, 3:6] / dt
        diag_value = np.diag(np.linalg.inv(m_hat_inv))
        m_hat = np.mean(diag_value)
        print("m_hat: ", m_hat)



        eps_A = np.linalg.norm(Ahat - self._A_star, ord=2)
        eps_B = np.linalg.norm(Bhat - self._B_star, ord=2)

        logger = self._get_logger()
        logger.info("prime: eps_A={}, eps_B={}, Jnom={}".format(eps_A, eps_B, Jnom))

        self._error_history.append((eps_A, eps_B))
        self._infinite_horizon_cost_history.append(Jnom)

        # reset the initial state to zero
        self._state_cur = np.zeros_like(self._state_cur)

        self._has_primed = True
        self.I_hat = I_hat
        self.m_hat = m_hat


    def _get_rng(self, rng):
        return np.random if rng is None else rng

    def _get_logger(self):
        # sub-classes are encouraged to override this to provide a logger with
        # better name context
        return logging.getLogger(__name__)

    def step(self, rng):
        """Run the simulation forward.

        """

        try:
            self._state_history
        except AttributeError:
            raise ValueError("Call reset() before calling step()")

        if not self._has_primed:
            raise ValueError("Call prime() before calling step()")

        # get next input
        inp = self._get_input(self._state_cur, rng)

        # obtain current cost
        cost = utils.quad_form(self._Q, self._state_cur) + \
               utils.quad_form(self._R, inp)

        # advance to next state
        xnext = self._A_star.dot(self._state_cur) + \
                self._B_star.dot(inp) + \
                self._sigma_w * self._get_rng(rng).normal(size=(self._n,))

        self._state_history.append(self._state_cur)
        self._input_history.append(inp)
        self._transition_history.append(xnext)

        if self._rls is not None:
            self._rls.update(self._state_cur, inp, xnext)
            Ahat, Bhat, _ = self._rls.get_estimate()
            eps_A = np.linalg.norm(Ahat - self._A_star, ord=2)
            eps_B = np.linalg.norm(Bhat - self._B_star, ord=2)
            self._rls_history.append((eps_A, eps_B))

        self._cost_history.append(cost)
        self._state_cur = xnext

        self._regret += (cost - self._J_star)
        self._iteration_idx += 1
        self._iteration_within_epoch_idx += 1

        self._on_iteration_completion()

        if self._should_terminate_epoch():
            self.complete_epoch(rng)

        return self.regret()

    def complete_epoch(self, rng):
        """Call this method to forcefully complete an epoch

        """

        self._on_epoch_completion()

        # TODO(stephentu):
        # implement a recursive LS estimator of (A, B)
        # so we can report the error along the entire trajectory
        # instead of just at the epoch boundaries.
        Ahat, Bhat, infinite_horizon_cost = self._design_controller(
                np.array(self._state_history),
                np.array(self._input_history),
                np.array(self._transition_history),
                rng)
        eps_A = np.linalg.norm(Ahat - self._A_star, ord=2)
        eps_B = np.linalg.norm(Bhat - self._B_star, ord=2)
        self._error_history.append((eps_A, eps_B))
        self._infinite_horizon_cost_history.append(infinite_horizon_cost)

        logger = self._get_logger()
        logger.info("Finished with epoch {}, which lasted for {} out of {} iterations".format(
            self._epoch_idx,
            self._iteration_within_epoch_idx,
            self._iteration_idx))
        logger.info("Regret={}, eps_A={}, eps_B={}, Jhat={}, elapsed_time_since_reset={}".format(
            self.regret(),
            eps_A,
            eps_B,
            infinite_horizon_cost,
            time.time() - self._last_reset_time))

        self._epoch_idx += 1
        self._epoch_history.append(self._iteration_within_epoch_idx)
        self._iteration_within_epoch_idx = 0


    def regret(self):
        return self._regret

    def get_statistics(self, iteration_based):
        if iteration_based:
            def expand(h):
                assert len(h) == len(self._epoch_history) + 1
                return np.array(list(it.chain.from_iterable([[v]*repeat for v, repeat in zip(h[:-1], self._epoch_history)])))
            def rel_err(Jhat):
                return (Jhat - self._J_star)/self._J_star
            return (expand(self._error_history), expand([rel_err(e) for e in self._infinite_horizon_cost_history]))
        else:
            return (np.array(self._error_history), np.array(self._infinite_horizon_cost_history))
