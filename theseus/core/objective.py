# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.
import itertools
import warnings
from collections import OrderedDict
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    Union,
)

import torch

from theseus.constants import DeviceType
from theseus.core.theseus_function import TheseusFunction
from theseus.geometry.manifold import Manifold

from .cost_function import CostFunction
from .cost_weight import CostWeight
from .variable import Variable


class ErrorMetric(Protocol):
    def __call__(self, error_vector: torch.Tensor) -> torch.Tensor:
        pass


def error_squared_norm_fn(error_vector: torch.Tensor) -> torch.Tensor:
    return (error_vector**2).sum(dim=1) / 2


# If dtype is None, uses torch.get_default_dtype()
class Objective:
    """An objective function to optimize.

    Defines the structure of an optimization problem in Theseus by aggregating
    :class:`cost functions <theseus.CostFunction>` into a single objective.
    The cost functions that comprise the final
    objective function are specified via the :meth:`add() <theseus.Objective.add>`
    method. Cost functions are responsible for registering their optimization and
    auxiliary variables, which are automatically added to the objective's list of
    variables when a cost function is added.
    Importantly, optimization variables must be instances of :class:`Manifold`
    subclasses, while auxiliary variables can be instances of
    any :class:`Variable` class.

    Args:
        dtype (optional[torch.dtype]): the data type to use for all variables. If
            ``None`` is passed, then uses ``torch.get_default_dtype()``.
        error_metric_fn (optional[callable]): a reference to a Python function used to
            aggregate cost functions into a single objective. Defaults to using the
            sum of squared costs. If given, it must receive a single tensor as input.
            The objective will use it to pass the batched concatenated error vector,
            will all cost function errors concatenated.
    """

    def __init__(
        self,
        dtype: Optional[torch.dtype] = None,
        error_metric_fn: Optional[ErrorMetric] = None,
        __allow_mixed_optim_aux_vars__: bool = False,  # experimental
    ):
        # maps variable names to the variable objects
        self.optim_vars: OrderedDict[str, Manifold] = OrderedDict()

        # maps variable names to variables objects, for optimization variables
        # that were registered when adding cost weights.
        self.cost_weight_optim_vars: OrderedDict[str, Manifold] = OrderedDict()

        # maps aux. variable names to the container objects
        self.aux_vars: OrderedDict[str, Variable] = OrderedDict()

        # maps variable name to variable, for any kind of variable added
        self._all_variables: OrderedDict[str, Variable] = OrderedDict()

        # maps cost function names to the cost function objects
        self.cost_functions: OrderedDict[str, CostFunction] = OrderedDict()

        # maps cost weights to the cost functions that use them
        # this is used when deleting cost function to check if the cost weight
        # variables can be deleted as well (when no other function uses them)
        self.cost_functions_for_weights: Dict[CostWeight, List[CostFunction]] = {}

        # ---- The following two methods are used just to get info from
        # ---- the objective, they don't affect the optimization logic.
        # a map from optimization variables to list of theseus functions it's
        # connected to
        self.functions_for_optim_vars: Dict[Manifold, List[TheseusFunction]] = {}

        # a map from all aux. variables to list of theseus functions it's connected to
        self.functions_for_aux_vars: Dict[Variable, List[TheseusFunction]] = {}

        self._batch_size: Optional[int] = None

        self.device: DeviceType = torch.device("cpu")

        self.dtype: Optional[torch.dtype] = dtype or torch.get_default_dtype()

        # this increases after every add/erase operation, and it's used to avoid
        # an optimizer to run on a stale version of the objective (since changing the
        # objective structure might break optimizer initialization).
        self.current_version = 0

        # ---- Callbacks for vectorization ---- #
        # This gets replaced when cost function vectorization is used.
        #
        # Normally, `_get_jacobians_iter()` returns an iterator over
        #  `self.cost_functions.values()`, so
        # that calling error() or jacobians() on the yielded cost functions
        # computes these quantities on demand.
        # But when vectorization is on, it will return this iterator that loops
        # over containers that serve cached jacobians and errors that have been
        # previously computed by the vectorization.
        self._vectorized_jacobians_iter: Optional[Iterable[CostFunction]] = None

        # Used to vectorize cost functions error + jacobians after an update
        # The results are cached so that the `self._get_jacobians_iter()` returns
        # them whenever called if no other updates have been done
        self._vectorization_run: Optional[Callable] = None

        # If vectorization is on, this gets replaced by a vectorized version
        # This method doesn't update the cache used by `self._get_jacobians_iter()`
        self._get_error_iter = self._get_error_iter_base

        # If vectorization is on, this will also handle vectorized containers
        self._vectorization_to: Optional[Callable] = None

        # If vectorization is on, this gets replaced by a vectorized version
        self._retract_method = Objective._retract_base

        # Keeps track of how many variable updates have been made to check
        # if vectorization should be updated
        self._num_updates_variables: Dict[str, int] = {}

        self._last_vectorization_has_grad = False

        self._vectorized = False

        # Computes an aggregation function for the error vector derived from costs
        # By default, this computes the squared norm of the error vector, divided by 2
        self._error_metric_fn = (
            error_metric_fn if error_metric_fn is not None else error_squared_norm_fn
        )

        self._allow_mixed_optim_aux_vars = __allow_mixed_optim_aux_vars__

    def _add_function_variables(
        self,
        function: TheseusFunction,
        optim_vars: bool = True,
        is_cost_weight: bool = False,
    ):
        if optim_vars:
            function_vars = function.optim_vars
            self_var_to_fn_map = self.functions_for_optim_vars
            self_vars_of_this_type = (
                self.cost_weight_optim_vars if is_cost_weight else self.optim_vars
            )
        else:
            function_vars = function.aux_vars  # type: ignore
            self_var_to_fn_map = self.functions_for_aux_vars  # type: ignore
            self_vars_of_this_type = self.aux_vars  # type: ignore

        for variable in function_vars:
            # Check that variables have name and correct dtype
            if variable.name is None:
                raise ValueError(
                    f"Variables added to an objective must be named, but "
                    f"{function.name} has an unnamed variable."
                )
            if variable.dtype != self.dtype:
                raise ValueError(
                    f"Tried to add variable {variable.name} with dtype "
                    f"{variable.dtype} but objective's dtype is {self.dtype}."
                )
            # Check that names are unique
            if variable.name in self._all_variables:
                if variable is not self._all_variables[variable.name]:
                    raise ValueError(
                        f"Two different variable objects with the "
                        f"same name ({variable.name}) are not allowed "
                        "in the same objective."
                    )
            else:
                self._all_variables[variable.name] = variable
                assert variable not in self_var_to_fn_map
                self_var_to_fn_map[variable] = []

            # add to either self.optim_vars,
            # self.cost_weight_optim_vars or self.aux_vars
            self_vars_of_this_type[variable.name] = variable

            if self._allow_mixed_optim_aux_vars and variable not in self_var_to_fn_map:
                self_var_to_fn_map[variable] = []

            # add to list of functions connected to this variable
            self_var_to_fn_map[variable].append(function)

    def add(self, cost_function: CostFunction):
        """Adds a cost function to the objective.

        When a cost function is added, this method goes over its list of registered
        optimization and auxiliary variables, and adds any of them to the objective's
        list of variables, as long as a variable with th4 same name hasn't been added
        before. If any of the cost function's variables has the same as that of
        a variable previously added to the objective, the method
        checks that they are referring to the same :class:`theseus.Variable`. If this
        is not the case, an error will be triggered. In other words, the objective
        expects to have a unique mapping between variable names and objects.

        The same procedure is followed for the cost function's weight.

        Args:
            cost_function (:class:`theseus.CostFunction`): the cost function to be
                added to the objective.

        .. warning::

            If a cost weight registers optimization variables that are not used in any
            :class:`theseus.CostFunction <CostFunction>` objects, these will **NOT**
            be added to the set of the objective's optimization variables; they will be
            kept in a separate container. The :meth:`update` method will check for this,
            and throw a warning whenever this happens. Also note that Theseus
            always considers cost weights as constants, even if their value depends on
            variables declared as optimization variables.
        """
        # adds the cost function if not already present
        if cost_function.name in self.cost_functions:
            if cost_function is not self.cost_functions[cost_function.name]:
                raise ValueError(
                    f"Two different cost function objects with the "
                    f"same name ({cost_function.name}) are not allowed "
                    "in the same objective."
                )
            else:
                warnings.warn(
                    "This cost function has already been added to the objective, "
                    "nothing to be done."
                )
        else:
            self.cost_functions[cost_function.name] = cost_function

        self.current_version += 1
        # ----- Book-keeping for the cost function ------- #
        # adds information about the optimization variables in this cost function
        self._add_function_variables(cost_function, optim_vars=True)

        # adds information about the auxiliary variables in this cost function
        self._add_function_variables(cost_function, optim_vars=False)

        if cost_function.weight not in self.cost_functions_for_weights:
            # ----- Book-keeping for the cost weight ------- #
            # adds information about the variables in this cost function's weight
            self._add_function_variables(
                cost_function.weight, optim_vars=True, is_cost_weight=True
            )
            # adds information about the auxiliary variables in this cost function's weight
            self._add_function_variables(
                cost_function.weight, optim_vars=False, is_cost_weight=True
            )

            self.cost_functions_for_weights[cost_function.weight] = []

            if cost_function.weight.num_optim_vars() > 0:
                raise RuntimeError(
                    f"The cost weight associated to {cost_function.name} receives one "
                    "or more optimization variables. Differentiating cost "
                    "weights with respect to optimization variables is not currently "
                    "supported, thus jacobians computed by our optimizers will be "
                    "incorrect. You may want to consider moving the weight computation "
                    "inside the cost function, so that the cost weight only receives "
                    "auxiliary variables."
                )

        self.cost_functions_for_weights[cost_function.weight].append(cost_function)

        optim_vars_names = [
            var.name
            for var in itertools.chain(
                cost_function.optim_vars, cost_function.weight.optim_vars
            )
        ]
        aux_vars_names = [
            var.name
            for var in itertools.chain(
                cost_function.aux_vars, cost_function.weight.aux_vars
            )
        ]
        if not self._allow_mixed_optim_aux_vars:
            dual_var_err_msg = (
                "Objective does not support a variable being both "
                + "an optimization variable and an auxiliary variable."
            )
            for optim_name in optim_vars_names:
                if self.has_aux_var(optim_name):
                    raise ValueError(dual_var_err_msg)
            for aux_name in aux_vars_names:
                if self.has_optim_var(aux_name):
                    raise ValueError(dual_var_err_msg)

    def get_cost_function(self, name: str) -> CostFunction:
        """Returns a reference to the cost function with the given name.

        Args:
            name (str): the name of the cost function to retrieve.

        Returns:
            CostFunction: the :class:`theseus.CostFunction` with the given name, if
            present. Otherwise ``None``.
        """
        return self.cost_functions.get(name, None)

    def has_cost_function(self, name: str) -> bool:
        """Checks if a cost function with the given name is in the objective.

        Args:
            name (str): the name of the cost function.

        Returns:
            bool: ``True`` if a function exists with the given name,
                ``False`` otherwise.
        """
        return name in self.cost_functions

    def has_optim_var(self, name: str) -> bool:
        """Checks if an optimization variable is used in the objective.

        Args:
            name (str): the name of the optimization variable.

        Returns:
            bool: ``True`` if an optimization variable with the given name exists in the
                objective (i.e., it's currently associated to at least one cost
                function in the objective). ``False`` otherwise.
        """
        return name in self.optim_vars

    def get_optim_var(self, name: str) -> Manifold:
        """Returns a reference to the optimization variable with the given name.

        Args:
            name (str): the name of the optimization variable to retrieve.

        Returns:
            Manifold: the :class:`theseus.Manifold` with the given name, if
            present. Otherwise ``None``.
        """
        return self.optim_vars.get(name, None)

    def has_aux_var(self, name: str) -> bool:
        """Checks if an auxiliary variable is used in the objective.

        Args:
            name (str): the name of the auxiliary variable.

        Returns:
            bool: ``True`` if an auxiliary variable with the given name exists in the
                objective (i.e., it's currently associated to at least one cost
                function or cost weight in the objective). ``False`` otherwise.
        """
        return name in self.aux_vars

    def get_aux_var(self, name: str) -> Variable:
        """Returns a reference to the auxiliary variable with the given name.

        Args:
            name (str): the name of the auxiliary variable to retrieve.

        Returns:
            Variable: the :class:`theseus.Variable` with the given name, if
            present. Otherwise ``None``.
        """
        return self.aux_vars.get(name, None)

    @property
    def batch_size(self) -> int:
        return self._batch_size

    def _erase_function_variables(
        self,
        function: TheseusFunction,
        optim_vars: bool = True,
        is_cost_weight: bool = False,
    ):
        if optim_vars:
            fn_var_list = function.optim_vars
            self_vars_of_this_type = (
                self.cost_weight_optim_vars if is_cost_weight else self.optim_vars
            )
            self_var_to_fn_map = self.functions_for_optim_vars
        else:
            fn_var_list = function.aux_vars  # type: ignore
            self_vars_of_this_type = self.aux_vars  # type: ignore
            self_var_to_fn_map = self.functions_for_aux_vars  # type: ignore

        for variable in fn_var_list:
            cost_fn_idx = self_var_to_fn_map[variable].index(function)
            # remove function from the variable's list of connected cost functions
            del self_var_to_fn_map[variable][cost_fn_idx]
            # if the variable has no other functions, remove it also
            if not self_var_to_fn_map[variable]:
                del self_var_to_fn_map[variable]
                del self_vars_of_this_type[variable.name]

    def erase(self, name: str):
        """Removes a cost function from the objective given its name

        Also removes any of its variables that are no longer associated to other
        functions (either cost functions, or cost weights).
        Does the same for the cost weight variables, but only if the weight is
        not associated to any other cost function.

        Args:
            name (str): the name of the cost function to erase.
        """
        self.current_version += 1
        if name in self.cost_functions:
            cost_function = self.cost_functions[name]
            # erase variables associated to this cost function (if needed)
            self._erase_function_variables(cost_function, optim_vars=True)
            self._erase_function_variables(cost_function, optim_vars=False)

            # delete cost function from list of cost functions connected to its weight
            cost_weight = cost_function.weight
            cost_fn_idx = self.cost_functions_for_weights[cost_weight].index(
                cost_function
            )
            del self.cost_functions_for_weights[cost_weight][cost_fn_idx]

            # No more cost functions associated to this weight, so can also delete
            if len(self.cost_functions_for_weights[cost_weight]) == 0:
                # erase its variables (if needed)
                self._erase_function_variables(
                    cost_weight, optim_vars=True, is_cost_weight=True
                )
                self._erase_function_variables(
                    cost_weight, optim_vars=False, is_cost_weight=True
                )
                del self.cost_functions_for_weights[cost_weight]

            # finally, delete the cost function
            del self.cost_functions[name]
        else:
            warnings.warn(
                "This cost function is not in the objective, nothing to be done."
            )

    @staticmethod
    def _get_functions_connected_to_var(
        variable: Union[str, Variable],
        objectives_var_container_dict: "OrderedDict[str, Variable]",
        var_to_cost_fn_map: Dict[Variable, List[TheseusFunction]],
        variable_type: str,
    ) -> List[TheseusFunction]:
        if isinstance(variable, str):
            if variable not in objectives_var_container_dict:
                raise ValueError(
                    f"{variable_type} named {variable} is not in the objective."
                )
            variable = objectives_var_container_dict[variable]
        if variable not in var_to_cost_fn_map:
            raise ValueError(
                f"{variable_type} {variable.name} is not in the objective."
            )
        return var_to_cost_fn_map[variable]

    def get_functions_connected_to_optim_var(
        self, variable: Union[Manifold, str]
    ) -> List[TheseusFunction]:
        """Gets a list of functions that depend on a given optimization variable.

        Args:
            variable (Union[Manifold, str]): the variable to query for. Can be an
                instance of :class:`theseus.Manifold` or a string, specifying the name.

        Returns:
            List[TheseusFunction]: all cost functions that depend on the optimization
                variable.
        """
        return Objective._get_functions_connected_to_var(
            variable,
            self.optim_vars,  # type: ignore
            self.functions_for_optim_vars,  # type: ignore
            "Optimization Variable",
        )

    def get_functions_connected_to_aux_var(
        self, aux_var: Union[Variable, str]
    ) -> List[TheseusFunction]:
        """Gets a list of functions that depend on a given auxiliary variable.

        Args:
            variable (Union[Variable, str]): the variable to query for. Can be an
                instance of :class:`theseus.Variable` or a string, specifying the name.

        Returns:
            List[TheseusFunction]: all cost functions that depend on the auxiliary
                variable.
        """
        return Objective._get_functions_connected_to_var(
            aux_var, self.aux_vars, self.functions_for_aux_vars, "Auxiliary Variable"
        )

    def dim(self) -> int:
        """Returns the dimension of the error vector.

        The dimension is equal to the sum of all cost functions' error dimensions.

        Returns:
            int: the error dimension.
        """
        err_dim = 0
        for cost_function in self.cost_functions.values():
            err_dim += cost_function.dim()
        return err_dim

    def size(self) -> Tuple[int, int]:
        """Returns the number of cost functions and variables in the objective.

        Returns:
            tuple[int, int]: the number of cost functions and optimization variables
                in the objective, in that order.
        """
        return len(self.cost_functions), len(self.optim_vars)

    def size_cost_functions(self) -> int:
        """Returns the number of cost functions in the objective.

        Returns:
            int: the number of cost functions in the objective.
        """
        return len(self.cost_functions)

    def size_variables(self) -> int:
        """Returns the number of optimization variables in the objective.

        Returns:
            int: the number of optimization variables in the objective.
        """
        return len(self.optim_vars)

    def size_aux_vars(self) -> int:
        """Returns the number of auxiliary variables in the objective.

        Returns:
            int: the number of auxiliary variables in the objective.
        """
        return len(self.aux_vars)

    def error(
        self,
        input_tensors: Optional[Dict[str, torch.Tensor]] = None,
        also_update: bool = False,
    ) -> torch.Tensor:
        """Evaluates the error vector.

        Args:
            input_tensors (Dict[str, torch.Tensor], optional): if given, it must be a
                dictionary mapping variable names to tensors; if a variable with the
                given name is registered in the objective, its tensor will be replaced
                with the one in the dictionary (possibly permanently, depending on the
                value of ``also_update``). Defaults to ``None``, in which case the error
                is evaluated using the current tensors stored in all registered
                variables.
            also_update (bool, optional): if ``True``, and ``input_tensors`` is given,
                the modified variables are permanently updated with the given tensors.
                Defaults to ``False``, in which case the variables are reverted to the
                previous tensors after the error is evaluated.

        Returns:
            torch.Tensor: a tensor of shape (batch_size x error_dim), with the
                concatenation of all cost functions error vectors. The order corresponds
                to the order in which cost functions were added to the objective.
        """
        old_tensors = {}
        if input_tensors is not None:
            if not also_update:
                for var in self.optim_vars:
                    old_tensors[var] = self.optim_vars[var].tensor
            # Update vectorization only if the input tensors will be used for a
            # persistent update.
            self.update(input_tensors=input_tensors, _update_vectorization=also_update)

        # Current behavior when vectorization is on, is to always compute the error.
        # One could potentially optimize by only recompute when `input_tensors`` is
        # not None, and serving from the jacobians cache. However, when robust cost
        # functions are present this results in incorrect rescaling of error terms
        # so we are currently avoiding this optimization. Optimizers also compute error
        # by passing `input_tensors`, so for optimizers the current version should be
        # good enough.
        error_vector = torch.cat(
            [cf.weighted_error() for cf in self._get_error_iter()], dim=1
        )

        if input_tensors is not None and not also_update:
            # This line reverts back to the old tensors if a persistent update wasn't
            # required (i.e., `also_update is False`).
            # In this case, we pass _update_vectorization=False because
            # vectorization wasn't updated in the first call to `update()`.
            self.update(old_tensors, _update_vectorization=False)
        return error_vector

    def error_metric(
        self,
        input_tensors: Optional[Dict[str, torch.Tensor]] = None,
        also_update: bool = False,
    ) -> torch.Tensor:
        """Aggregates all cost function errors into a (batched) scalar objective.

        Args:
            input_tensors (Dict[str, torch.Tensor], optional): if given, it must be a
                dictionary mapping variable names to tensors; if a variable with the
                given name is registered in the objective, its tensor will be replaced
                with the one in the dictionary (possibly permanently, depending on the
                value of ``also_update``). Defaults to ``None``, in which case the error
                is evaluated using the current tensors stored in all registered
                variables.
            also_update (bool, optional): if ``True``, and ``input_tensors`` is given,
                the modified variables are permanently updated with the given tensors.
                Defaults to ``False``, in which case the variables are reverted to the
                previous tensors after the error is evaluated.

        Returns:
            torch.Tensor: a tensor of shape (batch_size,) with the scalar value of
                the objective function.
        """
        return self._error_metric_fn(
            self.error(input_tensors=input_tensors, also_update=also_update)
        )

    def copy(self) -> "Objective":
        """Creates a new copy of this objective.

        Returns:
            Objective: another instance of :class:`theseus.Objective` with copies
                of all cost functions, weights, and variables, the same
                connectivity structure, and error metric.
        """
        new_objective = Objective(
            dtype=self.dtype, error_metric_fn=self._error_metric_fn
        )

        # First copy all individual cost weights
        old_to_new_cost_weight_map: Dict[CostWeight, CostWeight] = {}
        for cost_weight in self.cost_functions_for_weights:
            new_cost_weight = cost_weight.copy(
                new_name=cost_weight.name, keep_variable_names=True
            )
            old_to_new_cost_weight_map[cost_weight] = new_cost_weight

        # Now copy the cost functions and assign the corresponding cost weight copy
        new_cost_functions: List[CostFunction] = []
        for cost_function in self.cost_functions.values():
            new_cost_function = cost_function.copy(
                new_name=cost_function.name, keep_variable_names=True
            )
            # we assign the allocated weight copies to avoid saving duplicates
            new_cost_function.weight = old_to_new_cost_weight_map[cost_function.weight]
            new_cost_functions.append(new_cost_function)

        # Handle case where a variable is copied in 2+ cost functions or cost weights,
        # since only a single copy should be maintained by objective
        for cost_function in new_cost_functions:
            # CostFunction
            for i, var in enumerate(cost_function.optim_vars):
                if new_objective.has_optim_var(var.name):
                    cost_function.set_optim_var_at(
                        i, new_objective.optim_vars[var.name]
                    )
            for i, aux_var in enumerate(cost_function.aux_vars):
                if new_objective.has_aux_var(aux_var.name):
                    cost_function.set_aux_var_at(
                        i, new_objective.aux_vars[aux_var.name]
                    )
            # CostWeight
            for i, var in enumerate(cost_function.weight.optim_vars):
                if var.name in new_objective.cost_weight_optim_vars:
                    cost_function.weight.set_optim_var_at(
                        i, new_objective.cost_weight_optim_vars[var.name]
                    )
            for i, aux_var in enumerate(cost_function.weight.aux_vars):
                if new_objective.has_aux_var(aux_var.name):
                    cost_function.weight.set_aux_var_at(
                        i, new_objective.aux_vars[aux_var.name]
                    )
            new_objective.add(cost_function)
        return new_objective

    def __deepcopy__(self, memo):
        if id(self) in memo:
            return memo[id(self)]
        the_copy = self.copy()
        memo[id(self)] = the_copy
        return the_copy

    def _resolve_batch_size(self):
        self._batch_size = None

        def _get_batch_size(batch_sizes: Sequence[int]) -> int:
            unique_batch_sizes = set(batch_sizes)
            if len(unique_batch_sizes) == 1:
                return batch_sizes[0]
            if len(unique_batch_sizes) == 2:
                min_bs = min(unique_batch_sizes)
                max_bs = max(unique_batch_sizes)
                if min_bs == 1:
                    return max_bs
            raise ValueError("Provided tensors must be broadcastable.")

        batch_sizes = [v.tensor.shape[0] for v in self.optim_vars.values()]
        batch_sizes.extend([v.tensor.shape[0] for v in self.aux_vars.values()])
        self._batch_size = _get_batch_size(batch_sizes)

    # batch_ignore_mask is a boolean list where batch_ignore_mask[i] = 1 means
    # for any variable v, v[i] will *not* be updated. Shape must be equal to the
    # batch size.
    def update(
        self,
        input_tensors: Optional[Dict[str, torch.Tensor]] = None,
        batch_ignore_mask: Optional[torch.Tensor] = None,
        _update_vectorization: bool = True,
    ):
        """Updates all variables with the given input tensor dictionary.

        The behavior of this method can be summarized by the following pseudocode:

        .. code-block::

            for name, tensor in input_tensors.items():
                var = self.get_var_with_name(name).update(tensor)
            check_batch_size_consistency(self.all_variables)

        Any variables not included in the input tensors dictionary will retain their
        current tensors.

        After updating, the objective will modify its batch size
        property according to the resulting tensors. Therefore, all variable tensors
        must have a consistent batch size (either 1 or the same value as the others),
        after the update is completed. Note that this includes variables not referenced
        in the ``input_tensors`` dictionary.

        Args:
            input_tensors (Dict[str, torch.Tensor], optional): if given, it must be a
                dictionary mapping variable names to tensors; if a variable with the
                given name is registered in the objective, its tensor will be replaced
                with the one in the dictionary (possibly permanently, depending on the
                value of ``also_update``). Defaults to ``None``, in which case nothing
                will be updated. In both cases, the objective will resolve the
                batch size with whatever tensors are stored after updating.
            batch_ignore_mask (torch.Tensor, optional): an optional tensor of shape
                (batch_size,) of boolean type. Any ``True`` values indicate that
                this batch index should remain unchanged in all variables.
                Defaults to ``None``.

        Raises:
            ValueError: if tensors with inconsistent batch dimension are given.
        """
        input_tensors = input_tensors or {}
        for var_name, tensor in input_tensors.items():
            if tensor.ndim < 2:
                raise ValueError(
                    f"Input tensors must have a batch dimension and "
                    f"one ore more data dimensions, but tensor.ndim={tensor.ndim} for "
                    f"tensor with name {var_name}."
                )
            if var_name in self.optim_vars:
                self.optim_vars[var_name].update(
                    tensor, batch_ignore_mask=batch_ignore_mask
                )
            elif var_name in self.aux_vars:
                self.aux_vars[var_name].update(
                    tensor, batch_ignore_mask=batch_ignore_mask
                )
            elif var_name in self.cost_weight_optim_vars:
                self.cost_weight_optim_vars[var_name].update(
                    tensor, batch_ignore_mask=batch_ignore_mask
                )
                warnings.warn(
                    "Updated a variable declared as optimization, but it is "
                    "only associated to cost weights and not to any cost functions. "
                    "Theseus optimizers will only update optimization variables "
                    "that are associated to one or more cost functions."
                )
            else:
                warnings.warn(
                    f"Attempted to update a tensor with name {var_name}, "
                    "which is not associated to any variable in the objective."
                )

        # Check that the batch size of all tensors is consistent after update
        self._resolve_batch_size()
        if _update_vectorization:
            self.update_vectorization_if_needed()

    def _vectorization_needs_update(self):
        num_updates = {name: v._num_updates for name, v in self._all_variables.items()}
        needs = False
        if num_updates != self._num_updates_variables:
            self._num_updates_variables = num_updates
            needs = True

        if torch.is_grad_enabled():
            if not self._last_vectorization_has_grad:
                needs = True
        return needs

    def update_vectorization_if_needed(self):
        if self.vectorized and self._vectorization_needs_update():
            if self._batch_size is None:
                self._resolve_batch_size()
            self._vectorization_run()
            self._last_vectorization_has_grad = torch.is_grad_enabled()

    # iterates over cost functions
    def __iter__(self):
        return iter([cf for cf in self.cost_functions.values()])

    def _get_error_iter_base(self) -> Iterable:
        return iter(cf for cf in self.cost_functions.values())

    def _get_jacobians_iter(self) -> Iterable:
        self.update_vectorization_if_needed()
        if self.vectorized:
            return iter(cf for cf in self._vectorized_jacobians_iter)
        # No vectorization is used, just serve from cost functions
        return iter(cf for cf in self.cost_functions.values())

    def to(self, *args, **kwargs):
        """Applies torch.Tensor.to() to all cost functions in the objective."""
        for cost_function in self.cost_functions.values():
            cost_function.to(*args, **kwargs)
        device, dtype, *_ = torch._C._nn._parse_to(*args, **kwargs)
        self.device = device or self.device
        self.dtype = dtype or self.dtype
        if self._vectorization_to is not None:
            self._vectorization_to(*args, **kwargs)

    @staticmethod
    def _retract_base(
        delta: torch.Tensor,
        ordering: Iterable[Manifold],
        ignore_mask: Optional[torch.Tensor] = None,
        force_update: bool = False,
    ):
        var_idx = 0
        for var in ordering:
            new_var = var.retract(delta[:, var_idx : var_idx + var.dof()])
            if ignore_mask is None or force_update:
                var.update(new_var.tensor)
            else:
                var.update(new_var.tensor, batch_ignore_mask=ignore_mask)
            var_idx += var.dof()

    def retract_vars_sequence(
        self,
        delta: torch.Tensor,
        ordering: Iterable[Manifold],
        ignore_mask: Optional[torch.Tensor] = None,
        force_update: bool = False,
    ):
        """Retracts an ordered sequence of variables.

        The behavior of this method can be summarized by the following pseudocode:

        .. code-block::

            for var in ordering:
                var.retract(delta[var_idx])

        This function assumes that ``delta`` is constructed as follows:

        .. code-block::

            delta = torch.cat([delta_v1, delta_v2, ..., delta_vn], dim=-1)

        For an ordering ``[v1 v2 ... vn]``, and where
        ``delta_vi.shape = (batch_size, vi.dof())``

        Args:
            delta (torch.Tensor): the tensor to use for retract operation.
            ordering (Iterable[Manifold]): an ordered iterator of variables to retract.
                The order must be consistent with ``delta`` as explained above.
            ignore_mask (torch.Tensor, optional): An ignore mask for batch indices as
                in :meth:`update() <theseus.Objective.update>`. Defaults to ``None``.
            force_update (bool, optional): if ``True``, disregards the ``ignore_mask``.
                Defaults to ``False``.
        """
        self._retract_method(
            delta, ordering, ignore_mask=ignore_mask, force_update=force_update
        )
        # Updating immediately is useful, since it will keep grad history if
        # needed. Otherwise, with lazy waitng we can be in a situation where
        # vectorization is updated with torch.no_grad() (e.g., for error logging),
        # and then it has to be run again later when grad is back on.
        self.update_vectorization_if_needed()

    def _enable_vectorization(
        self,
        jacobians_iter: Iterable[CostFunction],
        vectorization_run_fn: Callable,
        vectorized_to: Callable,
        vectorized_retract_fn: Callable,
        error_iter_fn: Callable[[], Iterable[CostFunction]],
        enabler: Any,
    ):
        # Hacky way to make Vectorize a "friend" class
        assert (
            enabler.__module__ == "theseus.core.vectorizer"
            and enabler.__class__.__name__ == "Vectorize"
        )
        self._vectorized_jacobians_iter = jacobians_iter
        self._vectorization_run = vectorization_run_fn
        self._vectorization_to = vectorized_to
        self._retract_method = vectorized_retract_fn
        self._get_error_iter = error_iter_fn
        self._vectorized = True

    # Making public, since this should be a safe operation
    def disable_vectorization(self):
        self._vectorized_jacobians_iter = None
        self._vectorization_run = None
        self._vectorization_to = None
        self._retract_method = Objective._retract_base
        self._get_error_iter = self._get_error_iter_base
        self._vectorized = False

    @property
    def vectorized(self):
        assert (
            (not self._vectorized)
            == (self._vectorized_jacobians_iter is None)
            == (self._vectorization_run is None)
            == (self._vectorization_to is None)
            == (self._get_error_iter == self._get_error_iter_base)
            == (self._retract_method == Objective._retract_base)
        )
        return self._vectorized
