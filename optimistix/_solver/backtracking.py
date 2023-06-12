# Copyright 2023 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from typing import Any

import equinox as eqx
import jax
import jax.numpy as jnp
import lineax as lx
from jaxtyping import Array, Bool, Float, Int, PyTree, Scalar

from .._custom_types import Fn, LineSearchAux
from .._line_search import AbstractLineSearch
from .._misc import tree_full, tree_inner_prod, tree_where, tree_zeros_like
from .._solution import RESULTS


class _BacktrackingState(eqx.Module):
    f_val: Float[Array, ""]
    f0: Float[Array, ""]
    running_min: Array
    running_min_diff: PyTree[Array]
    vector: PyTree[Array]
    operator: lx.AbstractLinearOperator
    diff: PyTree[Array]
    compute_f0: Bool[Array, ""]
    result: RESULTS
    step: Int[Array, ""]


class BacktrackingArmijo(AbstractLineSearch[_BacktrackingState]):
    gauss_newton: bool
    backtrack_slope: float
    decrease_factor: float

    def __post_init__(self):
        eps = jnp.finfo(jnp.float64).eps
        if self.decrease_factor < eps:
            raise ValueError(
                "The decrease factor of linesearch must be greater than 0!"
            )

    def first_init(
        self,
        vector: PyTree[Array],
        operator: lx.AbstractLinearOperator,
        options: dict[str, Any],
    ) -> Scalar:
        try:
            init_size = options["init_line_search"]
        except KeyError:
            init_size = jnp.array(1 / self.decrease_factor)
        return init_size

    def init(
        self,
        fn: Fn[Scalar, Scalar, LineSearchAux],
        y: Scalar,
        args: PyTree,
        options: dict[str, Any],
        f_struct: PyTree[jax.ShapeDtypeStruct],
        aux_struct: PyTree[jax.ShapeDtypeStruct],
        tags: frozenset[object],
    ) -> _BacktrackingState:
        try:
            vector = options["vector"]
        except KeyError:
            raise ValueError("vector must be passed vector " "via `options['vector']`")
        try:
            operator = options["operator"]
        except KeyError:
            raise ValueError(
                "operator must be passed operator " "via `options['operator']`"
            )
        try:
            diff0 = tree_zeros_like(options["diff"])
        except KeyError:
            assert False
        try:
            f0 = options["f0"]
            compute_f0 = options["compute_f0"]
        except KeyError:
            f0 = tree_full(f_struct, jnp.inf)
            compute_f0 = jnp.array(True)
        return _BacktrackingState(
            f_val=f0,
            f0=f0,
            running_min=f0,
            running_min_diff=diff0,
            vector=vector,
            operator=operator,
            diff=diff0,
            compute_f0=compute_f0,
            result=RESULTS.successful,
            step=jnp.array(0),
        )

    def step(
        self,
        fn: Fn[Scalar, Scalar, LineSearchAux],
        y: Scalar,
        args: PyTree,
        options: dict[str, Any],
        state: _BacktrackingState,
        tags: frozenset[object],
    ) -> tuple[Scalar, _BacktrackingState, LineSearchAux]:
        delta = jnp.where(state.compute_f0, jnp.array(0.0), y)
        (f_val, (_, diff, aux, result, _)) = fn(delta, args)
        f0 = jnp.where(state.compute_f0, f_val, state.f0)

        running_min = jnp.where(f_val < state.running_min, f_val, state.running_min)
        running_min_diff = tree_where(
            f_val < state.running_min, diff, state.running_min_diff
        )

        new_y = self.decrease_factor * y
        new_state = _BacktrackingState(
            f_val,
            f0,
            running_min,
            running_min_diff,
            state.vector,
            state.operator,
            diff,
            jnp.array(False),
            result,
            state.step + 1,
        )
        return (
            new_y,
            new_state,
            (
                running_min,
                running_min_diff,
                aux,
                result,
                jnp.array(1 / self.decrease_factor),
            ),
        )

    def terminate(
        self,
        fn: Fn[Scalar, Scalar, LineSearchAux],
        y: Scalar,
        args: PyTree,
        options: dict[str, Any],
        state: _BacktrackingState,
        tags: frozenset[object],
    ) -> tuple[Bool[Array, ""], RESULTS]:
        result = RESULTS.where(
            jnp.isfinite(y),
            state.result,
            RESULTS.nonlinear_divergence,
        )
        # `J^T r` is the grad in a Gauss-Newton problem, where J is the Jacobian
        # and r the residual
        if self.gauss_newton:
            grad = state.operator.transpose().mv(state.vector)
        else:
            grad = state.vector
        # Reduction should be at least linear, this is the Armijo condition
        predicted_reduction = tree_inner_prod(grad, state.diff)
        predicted_reduction_is_neg = predicted_reduction < 0
        finished = state.f_val < state.f0 + self.backtrack_slope * predicted_reduction
        finished = finished & (state.step > 1) & predicted_reduction_is_neg
        return finished, result

    def buffers(self, state: _BacktrackingState) -> tuple[()]:
        return ()
