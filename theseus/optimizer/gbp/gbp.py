# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import abc
import math
from dataclasses import dataclass
from itertools import count
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import torch

import theseus as th
import theseus.constants
from theseus.core import CostFunction, Objective
from theseus.geometry import Manifold
from theseus.optimizer import Optimizer, VariableOrdering
from theseus.optimizer.nonlinear.nonlinear_optimizer import (
    BackwardMode,
    NonlinearOptimizerInfo,
    NonlinearOptimizerStatus,
)

"""
TODO
 - damping for lie algebra vars
 - solving inverse problem to compute message mean
 - handle batch dim
"""


"""
Utitily functions
"""


# Same of NonlinearOptimizerParams but without step size
@dataclass
class GBPOptimizerParams:
    abs_err_tolerance: float
    rel_err_tolerance: float
    max_iterations: int

    def update(self, params_dict):
        for param, value in params_dict.items():
            if hasattr(self, param):
                setattr(self, param, value)
            else:
                raise ValueError(f"Invalid nonlinear optimizer parameter {param}.")


def synchronous_schedule(max_iters, n_edges) -> torch.Tensor:
    return torch.full([max_iters, n_edges], True)


def random_schedule(max_iters, n_edges) -> torch.Tensor:
    schedule = torch.full([max_iters, n_edges], False)
    ixs = torch.randint(0, n_edges, [max_iters])
    schedule[torch.arange(max_iters), ixs] = True
    return schedule


class ManifoldGaussian:
    _ids = count(0)

    def __init__(
        self,
        mean: Sequence[Manifold],
        precision: Optional[torch.Tensor] = None,
        name: Optional[str] = None,
    ):
        self._id = next(ManifoldGaussian._ids)
        if name:
            self.name = name
        else:
            self.name = f"{self.__class__.__name__}__{self._id}"

        dof = 0
        for v in mean:
            dof += v.dof()
        self._dof = dof

        self.mean = mean
        self.precision = torch.zeros(mean[0].shape[0], self.dof, self.dof).to(
            dtype=mean[0].dtype, device=mean[0].device
        )

    @property
    def dof(self) -> int:
        return self._dof

    @property
    def device(self) -> torch.device:
        return self.precision[0].device

    @property
    def dtype(self) -> torch.dtype:
        return self.precision[0].dtype

    # calls to() on the internal tensors
    def to(self, *args, **kwargs):
        for var in self.mean:
            var = var.to(*args, **kwargs)
        self.precision = self.precision.to(*args, **kwargs)

    def copy(self, new_name: Optional[str] = None) -> "ManifoldGaussian":
        if not new_name:
            new_name = f"{self.name}_copy"
        mean_copy = [var.copy() for var in self.mean]
        return ManifoldGaussian(mean_copy, name=new_name)

    def __deepcopy__(self, memo):
        if id(self) in memo:
            return memo[id(self)]
        the_copy = self.copy()
        memo[id(self)] = the_copy
        return the_copy

    def update(
        self,
        mean: Optional[Sequence[Manifold]] = None,
        precision: Optional[torch.Tensor] = None,
    ):
        if mean is not None:
            if len(mean) != len(self.mean):
                raise ValueError(
                    f"Tried to update mean with sequence of different"
                    f"lenght to original mean sequence. Given {len(mean)}. "
                    f"Expected: {len(self.mean)}"
                )
            for i in range(len(self.mean)):
                self.mean[i].update(mean[i])

        if precision is not None:
            if precision.shape != self.precision.shape:
                raise ValueError(
                    f"Tried to update precision with data "
                    f"incompatible with original tensor shape. Given {precision.shape}. "
                    f"Expected: {self.precision.shape}"
                )
            if precision.dtype != self.dtype:
                raise ValueError(
                    f"Tried to update using tensor of dtype {precision.dtype} but precision "
                    f"has dtype {self.dtype}."
                )

            self.precision = precision


class Marginal(ManifoldGaussian):
    pass


class Message(ManifoldGaussian):
    pass


"""
Local and retract
These could be implemented as methods in Manifold class
"""


# Transforms message gaussian to tangent plane at var
# if return_mean is True, return the (mean, lam) else return (eta, lam).
# Generalises the local function by transforming the covariance as well as mean.
def local_gaussian(
    mess: Message,
    var: th.LieGroup,
    return_mean: bool = True,
) -> Tuple[torch.Tensor, torch.Tensor]:
    # mean_tp is message mean in tangent space / plane at var
    mean_tp = var.local(mess.mean[0])

    jac: List[torch.Tensor] = []
    th.exp_map(var, mean_tp, jacobians=jac)

    # lam_tp is the precision matrix in the tangent plane
    lam_tp = torch.bmm(torch.bmm(jac[0].transpose(-1, -2), mess.precision), jac[0])

    if return_mean:
        return mean_tp, lam_tp

    else:
        eta_tp = torch.matmul(lam_tp, mean_tp.unsqueeze(-1)).squeeze(-1)
        return eta_tp, lam_tp


# Transforms Gaussian in the tangent plane at var to Gaussian where the mean
# is a group element and the precision matrix is defined in the tangent plane
# at the mean.
# Generalises the retract function by transforming the covariance as well as mean.
# out_gauss is the transformed Gaussian that is updated in place.
def retract_gaussian(
    mean_tp: torch.Tensor,
    precision_tp: torch.Tensor,
    var: th.LieGroup,
    out_gauss: ManifoldGaussian,
):
    mean = var.retract(mean_tp)

    jac: List[torch.Tensor] = []
    th.exp_map(var, mean_tp, jacobians=jac)
    inv_jac = torch.inverse(jac[0])
    precision = torch.bmm(torch.bmm(inv_jac.transpose(-1, -2), precision_tp), inv_jac)

    out_gauss.update(mean=[mean], precision=precision)


class CostFunctionOrdering:
    def __init__(self, objective: Objective, default_order: bool = True):
        self.objective = objective
        self._cf_order: List[CostFunction] = []
        self._cf_name_to_index: Dict[str, int] = {}
        if default_order:
            self._compute_default_order(objective)

    def _compute_default_order(self, objective: Objective):
        assert not self._cf_order and not self._cf_name_to_index
        cur_idx = 0
        for cf_name, cf in objective.cost_functions.items():
            if cf_name in self._cf_name_to_index:
                continue
            self._cf_order.append(cf)
            self._cf_name_to_index[cf_name] = cur_idx
            cur_idx += 1

    def index_of(self, key: str) -> int:
        return self._cf_name_to_index[key]

    def __getitem__(self, index) -> CostFunction:
        return self._cf_order[index]

    def __iter__(self):
        return iter(self._cf_order)

    def append(self, cf: CostFunction):
        if cf in self._cf_order:
            raise ValueError(
                f"Cost Function {cf.name} has already been added to the order."
            )
        if cf.name not in self.objective.cost_functions:
            raise ValueError(
                f"Cost Function {cf.name} is not a cost function for the objective."
            )
        self._cf_order.append(cf)
        self._cf_name_to_index[cf.name] = len(self._cf_order) - 1

    def remove(self, cf: CostFunction):
        self._cf_order.remove(cf)
        del self._cf_name_to_index[cf.name]

    def extend(self, cfs: Sequence[CostFunction]):
        for cf in cfs:
            self.append(cf)

    @property
    def complete(self):
        return len(self._cf_order) == self.objective.size_variables()


"""
GBP functions
"""


class Factor:
    _ids = count(0)

    def __init__(
        self,
        cf: CostFunction,
        name: Optional[str] = None,
    ):
        self._id = next(Factor._ids)
        if name:
            self.name = name
        else:
            self.name = f"{self.__class__.__name__}__{self._id}"

        self.cf = cf

        batch_size = cf.optim_var_at(0).shape[0]
        self._dof = sum([var.dof() for var in cf.optim_vars])
        self.potential_eta = torch.zeros(batch_size, self.dof).to(
            dtype=cf.optim_var_at(0).dtype, device=cf.optim_var_at(0).device
        )
        self.potential_lam = torch.zeros(batch_size, self.dof, self.dof).to(
            dtype=cf.optim_var_at(0).dtype, device=cf.optim_var_at(0).device
        )
        self.lin_point = [
            var.copy(new_name=f"{cf.name}_{var.name}_lp") for var in cf.optim_vars
        ]

        self.linearize()

    # Linearizes factors at current belief if beliefs have deviated
    # from the linearization point by more than the threshold.
    def linearize(
        self,
        relin_threshold: float = None,
        lie=True,
    ):
        do_lin = False
        if relin_threshold is None:
            do_lin = True
        else:
            lp_dists = [
                lp.local(self.cf.optim_var_at(j)).norm()
                for j, lp in enumerate(self.lin_point)
            ]
            do_lin = np.max(lp_dists) > relin_threshold

        if do_lin:
            J, error = self.cf.weighted_jacobians_error()
            J_stk = torch.cat(J, dim=-1)

            lam = torch.bmm(J_stk.transpose(-2, -1), J_stk)

            optim_vars_stk = torch.cat([v.data for v in self.cf.optim_vars], dim=-1)
            eta = -torch.matmul(J_stk.transpose(-2, -1), error.unsqueeze(-1))
            if lie is False:
                eta = eta + torch.matmul(lam, optim_vars_stk.unsqueeze(-1))
            eta = eta.squeeze(-1)

            self.potential_eta = eta
            self.potential_lam = lam

            for j, var in enumerate(self.cf.optim_vars):
                self.lin_point[j].update(var.data)

    # Compute all outgoing messages from the factor.
    def comp_mess(
        self,
        vtof_msgs,
        ftov_msgs,
        damping,
    ):
        num_optim_vars = self.cf.num_optim_vars()
        new_messages = []

        sdim = 0
        for v in range(num_optim_vars):
            eta_factor = self.potential_eta.clone()[0]
            lam_factor = self.potential_lam.clone()[0]

            # Take product of factor with incoming messages.
            # Convert mesages to tangent space at linearisation point.
            start = 0
            for i in range(num_optim_vars):
                var_dofs = self.cf.optim_var_at(i).dof()
                if i != v:
                    eta_mess, lam_mess = local_gaussian(
                        vtof_msgs[i], self.lin_point[i], return_mean=False
                    )
                    eta_factor[start : start + var_dofs] += eta_mess[0]
                    lam_factor[
                        start : start + var_dofs, start : start + var_dofs
                    ] += lam_mess[0]
                start += var_dofs

            # Divide up parameters of distribution
            dofs = self.cf.optim_var_at(v).dof()
            eo = eta_factor[sdim : sdim + dofs]
            eno = np.concatenate((eta_factor[:sdim], eta_factor[sdim + dofs :]))

            loo = lam_factor[sdim : sdim + dofs, sdim : sdim + dofs]
            lono = np.concatenate(
                (
                    lam_factor[sdim : sdim + dofs, :sdim],
                    lam_factor[sdim : sdim + dofs, sdim + dofs :],
                ),
                axis=1,
            )
            lnoo = np.concatenate(
                (
                    lam_factor[:sdim, sdim : sdim + dofs],
                    lam_factor[sdim + dofs :, sdim : sdim + dofs],
                ),
                axis=0,
            )
            lnono = np.concatenate(
                (
                    np.concatenate(
                        (lam_factor[:sdim, :sdim], lam_factor[:sdim, sdim + dofs :]),
                        axis=1,
                    ),
                    np.concatenate(
                        (
                            lam_factor[sdim + dofs :, :sdim],
                            lam_factor[sdim + dofs :, sdim + dofs :],
                        ),
                        axis=1,
                    ),
                ),
                axis=0,
            )

            new_mess_lam = loo - lono @ np.linalg.inv(lnono) @ lnoo
            new_mess_eta = eo - lono @ np.linalg.inv(lnono) @ eno

            # damping in tangent space at linearisation point as message
            # is already in this tangent space. Could equally do damping
            # in the tangent space of the new or old message mean.
            prev_mess_mean, prev_mess_lam = local_gaussian(
                ftov_msgs[v], self.lin_point[v], return_mean=True
            )
            # mean damping
            if new_mess_lam.count_nonzero() != 0:
                new_mess_mean = torch.matmul(torch.inverse(new_mess_lam), new_mess_eta)
                new_mess_mean = (1 - damping[v]) * new_mess_mean + damping[
                    v
                ] * prev_mess_mean[0]
                new_mess_eta = torch.matmul(new_mess_lam, new_mess_mean)

            if new_mess_lam.count_nonzero() == 0:
                new_mess = ManifoldGaussian([self.cf.optim_var_at(v).copy()])
                new_mess.mean[0].data[:] = 0.0
            else:
                new_mess_mean = torch.matmul(torch.inverse(new_mess_lam), new_mess_eta)
                new_mess_mean = new_mess_mean[None, ...]
                new_mess_lam = new_mess_lam[None, ...]

                new_mess = ManifoldGaussian([self.cf.optim_var_at(v).copy()])
                retract_gaussian(
                    new_mess_mean, new_mess_lam, self.lin_point[v], new_mess
                )
            new_messages.append(new_mess)

            sdim += dofs

        # update messages
        for v in range(num_optim_vars):
            ftov_msgs[v].update(
                mean=new_messages[v].mean, precision=new_messages[v].precision
            )

        return new_messages

    @property
    def dof(self) -> int:
        return self._dof


# Follows notation from https://arxiv.org/pdf/2202.03314.pdf
class GaussianBeliefPropagation(Optimizer, abc.ABC):
    def __init__(
        self,
        objective: Objective,
        abs_err_tolerance: float = 1e-10,
        rel_err_tolerance: float = 1e-8,
        max_iterations: int = 20,
    ):
        super().__init__(objective)

        # ordering is required to identify which messages to send where
        self.ordering = VariableOrdering(objective, default_order=True)
        self.cf_ordering = CostFunctionOrdering(objective)

        self.params = GBPOptimizerParams(
            abs_err_tolerance, rel_err_tolerance, max_iterations
        )

        self.n_edges = sum([cf.num_optim_vars() for cf in self.cf_ordering])

        # create array for indexing the messages
        var_ixs_nested = [
            [self.ordering.index_of(var.name) for var in cf.optim_vars]
            for cf in self.cf_ordering
        ]
        var_ixs = [item for sublist in var_ixs_nested for item in sublist]
        self.var_ix_for_edges = torch.tensor(var_ixs).long()

    """
    Copied and slightly modified from nonlinear optimizer class
    """

    def set_params(self, **kwargs):
        self.params.update(kwargs)

    def _check_convergence(self, err: torch.Tensor, last_err: torch.Tensor):
        assert not torch.is_grad_enabled()
        if err.abs().mean() < theseus.constants.EPS:
            return torch.ones_like(err).bool()

        abs_error = (last_err - err).abs()
        rel_error = abs_error / last_err
        return (abs_error < self.params.abs_err_tolerance).logical_or(
            rel_error < self.params.rel_err_tolerance
        )

    def _maybe_init_best_solution(
        self, do_init: bool = False
    ) -> Optional[Dict[str, torch.Tensor]]:
        if not do_init:
            return None
        solution_dict = {}
        for var in self.ordering:
            solution_dict[var.name] = var.data.detach().clone().cpu()
        return solution_dict

    def _init_info(
        self, track_best_solution: bool, track_err_history: bool, verbose: bool
    ) -> NonlinearOptimizerInfo:
        with torch.no_grad():
            last_err = self.objective.error_squared_norm() / 2
        best_err = last_err.clone() if track_best_solution else None
        if track_err_history:
            err_history = (
                torch.ones(self.objective.batch_size, self.params.max_iterations + 1)
                * math.inf
            )
            assert last_err.grad_fn is None
            err_history[:, 0] = last_err.clone().cpu()
        else:
            err_history = None
        return NonlinearOptimizerInfo(
            best_solution=self._maybe_init_best_solution(do_init=track_best_solution),
            last_err=last_err,
            best_err=best_err,
            status=np.array(
                [NonlinearOptimizerStatus.START] * self.objective.batch_size
            ),
            converged_iter=torch.zeros_like(last_err, dtype=torch.long),
            best_iter=torch.zeros_like(last_err, dtype=torch.long),
            err_history=err_history,
        )

    def _update_info(
        self,
        info: NonlinearOptimizerInfo,
        current_iter: int,
        err: torch.Tensor,
        converged_indices: torch.Tensor,
    ):
        info.converged_iter += 1 - converged_indices.long()
        if info.err_history is not None:
            assert err.grad_fn is None
            info.err_history[:, current_iter + 1] = err.clone().cpu()

        if info.best_solution is not None:
            # Only copy best solution if needed (None means track_best_solution=False)
            assert info.best_err is not None
            good_indices = err < info.best_err
            info.best_iter[good_indices] = current_iter
            for var in self.ordering:
                info.best_solution[var.name][good_indices] = (
                    var.data.detach().clone()[good_indices].cpu()
                )

            info.best_err = torch.minimum(info.best_err, err)

        converged_indices = self._check_convergence(err, info.last_err)
        info.status[
            np.array(converged_indices.detach().cpu())
        ] = NonlinearOptimizerStatus.CONVERGED

    # Modifies the (no grad) info in place to add data of grad loop info
    def _merge_infos(
        self,
        grad_loop_info: NonlinearOptimizerInfo,
        num_no_grad_iter: int,
        backward_num_iterations: int,
        info: NonlinearOptimizerInfo,
    ):
        # Concatenate error histories
        if info.err_history is not None:
            info.err_history[:, num_no_grad_iter:] = grad_loop_info.err_history[
                :, : backward_num_iterations + 1
            ]
        # Merge best solution and best error
        if info.best_solution is not None:
            best_solution = {}
            best_err_no_grad = info.best_err
            best_err_grad = grad_loop_info.best_err
            idx_no_grad = best_err_no_grad < best_err_grad
            best_err = torch.minimum(best_err_no_grad, best_err_grad)
            for var_name in info.best_solution:
                sol_no_grad = info.best_solution[var_name]
                sol_grad = grad_loop_info.best_solution[var_name]
                best_solution[var_name] = torch.where(
                    idx_no_grad, sol_no_grad, sol_grad
                )
            info.best_solution = best_solution
            info.best_err = best_err

        # Merge the converged status into the info from the detached loop,
        M = info.status == NonlinearOptimizerStatus.MAX_ITERATIONS
        assert np.all(
            (grad_loop_info.status[M] == NonlinearOptimizerStatus.MAX_ITERATIONS)
            | (grad_loop_info.status[M] == NonlinearOptimizerStatus.CONVERGED)
        )
        info.status[M] = grad_loop_info.status[M]
        info.converged_iter[M] = (
            info.converged_iter[M] + grad_loop_info.converged_iter[M]
        )
        # If didn't coverge in either loop, remove misleading converged_iter value
        info.converged_iter[
            M & (grad_loop_info.status == NonlinearOptimizerStatus.MAX_ITERATIONS)
        ] = -1

    """
    GBP functions
    """

    def _pass_var_to_fac_messages(
        self,
        ftov_msgs,
        vtof_msgs,
        update_belief=True,
    ):
        for i, var in enumerate(self.ordering):

            # Collect all incoming messages in the tangent space at the current belief
            taus = []  # message means
            lams_tp = []  # message lams
            for j, msg in enumerate(ftov_msgs):
                if self.var_ix_for_edges[j] == i:
                    tau, lam_tp = local_gaussian(msg, var, return_mean=True)
                    taus.append(tau[None, ...])
                    lams_tp.append(lam_tp[None, ...])

            taus = torch.cat(taus)
            lams_tp = torch.cat(lams_tp)

            lam_tau = lams_tp.sum(dim=0)

            # Compute outgoing messages
            ix = 0
            for j, msg in enumerate(ftov_msgs):
                if self.var_ix_for_edges[j] == i:
                    taus_inc = torch.cat((taus[:ix], taus[ix + 1 :]))
                    lams_inc = torch.cat((lams_tp[:ix], lams_tp[ix + 1 :]))

                    lam_a = lams_inc.sum(dim=0)
                    if lam_a.count_nonzero() == 0:
                        vtof_msgs[j].mean[0].data[:] = 0.0
                        vtof_msgs[j].precision = lam_a
                    else:
                        inv_lam_a = torch.inverse(lam_a)
                        sum_taus = torch.matmul(lams_inc, taus_inc.unsqueeze(-1)).sum(
                            dim=0
                        )
                        tau_a = torch.matmul(inv_lam_a, sum_taus).squeeze(-1)
                        retract_gaussian(tau_a, lam_a, var, vtof_msgs[j])
                    ix += 1

            # update belief mean and variance
            # if no incoming messages then leave current belief unchanged
            if update_belief and lam_tau.count_nonzero() != 0:
                inv_lam_tau = torch.inverse(lam_tau)
                sum_taus = torch.matmul(lams_tp, taus.unsqueeze(-1)).sum(dim=0)
                tau = torch.matmul(inv_lam_tau, sum_taus).squeeze(-1)

                retract_gaussian(tau, lam_tau, var, self.beliefs[i])

    def _pass_fac_to_var_messages(
        self,
        vtof_msgs,
        ftov_msgs,
        schedule: torch.Tensor,
        damping: torch.Tensor,
    ):
        start = 0
        for factor in self.factors:
            num_optim_vars = factor.cf.num_optim_vars()

            factor.linearize(relin_threshold=None)

            factor.comp_mess(
                vtof_msgs[start : start + num_optim_vars],
                ftov_msgs[start : start + num_optim_vars],
                damping[start : start + num_optim_vars],
            )

            start += num_optim_vars

    """
    Optimization loop functions
    """

    # loop for the iterative optimizer
    def _optimize_loop(
        self,
        start_iter: int,
        num_iter: int,
        info: NonlinearOptimizerInfo,
        verbose: bool,
        truncated_grad_loop: bool,
        relin_threshold: float = 0.1,
        damping: float = 0.0,
        dropout: float = 0.0,
        schedule: torch.Tensor = None,
        **kwargs,
    ):
        if damping > 1.0 or damping < 0.0:
            raise ValueError(f"Damping must be in between 0 and 1. Got {damping}.")
        if dropout > 1.0 or dropout < 0.0:
            raise ValueError(
                f"Dropout probability must be in between 0 and 1. Got {dropout}."
            )
        if schedule is None:
            schedule = random_schedule(self.params.max_iterations, self.n_edges)
        elif schedule.dtype != torch.bool:
            raise ValueError(
                f"Schedule must be of dtype {torch.bool} but has dtype {schedule.dtype}."
            )
        elif schedule.shape != torch.Size([self.params.max_iterations, self.n_edges]):
            raise ValueError(
                f"Schedule must have shape [max_iterations, num_edges]. "
                f"Should be {torch.Size([self.params.max_iterations, self.n_edges])} "
                f"but got {schedule.shape}."
            )

        # initialise messages with zeros
        vtof_msgs: List[Message] = []
        ftov_msgs: List[Message] = []
        for cf in self.cf_ordering:
            for var in cf.optim_vars:
                vtof_msg_mu = var.copy(new_name=f"msg_{var.name}_to_{cf.name}")
                # mean of initial message doesn't matter as long as precision is zero
                vtof_msg_mu.data[:] = 0
                ftov_msg_mu = vtof_msg_mu.copy(new_name=f"msg_{cf.name}_to_{var.name}")
                vtof_msgs.append(Message([vtof_msg_mu]))
                ftov_msgs.append(Message([ftov_msg_mu]))

        # initialise Marginal for belief
        self.beliefs: List[Marginal] = []
        for var in self.ordering:
            self.beliefs.append(Marginal([var]))

        # compute factor potentials for the first time
        self.factors: List[Factor] = []
        for cf in self.cf_ordering:
            self.factors.append(Factor(cf))

        converged_indices = torch.zeros_like(info.last_err).bool()
        for it_ in range(start_iter, start_iter + num_iter):

            # damping
            # damping = self.gbp_settings.get_damping(iters_since_relin)
            damping_arr = torch.full([self.n_edges], damping)

            # dropout can be implemented through damping
            if dropout != 0.0:
                dropout_ixs = torch.rand(self.n_edges) < dropout
                damping_arr[dropout_ixs] = 1.0

            self._pass_fac_to_var_messages(
                vtof_msgs,
                ftov_msgs,
                schedule[it_],
                damping_arr,
            )

            self._pass_var_to_fac_messages(
                ftov_msgs,
                vtof_msgs,
                update_belief=True,
            )

            # check for convergence
            if it_ > 0:
                with torch.no_grad():
                    err = self.objective.error_squared_norm() / 2
                    self._update_info(info, it_, err, converged_indices)
                    if verbose:
                        print(
                            f"GBP. Iteration: {it_+1}. " f"Error: {err.mean().item()}"
                        )
                    converged_indices = self._check_convergence(err, info.last_err)
                    info.status[
                        converged_indices.cpu().numpy()
                    ] = NonlinearOptimizerStatus.CONVERGED
                    if converged_indices.all() and it_ > 1:
                        break  # nothing else will happen at this point
                    info.last_err = err

        info.status[
            info.status == NonlinearOptimizerStatus.START
        ] = NonlinearOptimizerStatus.MAX_ITERATIONS
        return info

    # `track_best_solution` keeps a **detached** copy (as in no gradient info)
    # of the best variables found, but it is optional to avoid unnecessary copying
    # if this is not needed
    def _optimize_impl(
        self,
        track_best_solution: bool = False,
        track_err_history: bool = False,
        verbose: bool = False,
        backward_mode: BackwardMode = BackwardMode.FULL,
        damping: float = 0.0,
        dropout: float = 0.0,
        schedule: torch.Tensor = None,
        **kwargs,
    ) -> NonlinearOptimizerInfo:
        with torch.no_grad():
            info = self._init_info(track_best_solution, track_err_history, verbose)

        if verbose:
            print(
                f"GBP optimizer. Iteration: 0. " f"Error: {info.last_err.mean().item()}"
            )

        grad = False
        if backward_mode == BackwardMode.FULL:
            grad = True

        with torch.set_grad_enabled(grad):
            info = self._optimize_loop(
                start_iter=0,
                num_iter=self.params.max_iterations,
                info=info,
                verbose=verbose,
                truncated_grad_loop=False,
                damping=damping,
                dropout=dropout,
                schedule=schedule,
                **kwargs,
            )

            # If didn't coverge, remove misleading converged_iter value
            info.converged_iter[
                info.status == NonlinearOptimizerStatus.MAX_ITERATIONS
            ] = -1
            return info
