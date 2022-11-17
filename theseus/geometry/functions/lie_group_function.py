# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import torch
import abc

from typing import Optional, List


class LieGroupAdjoint(torch.autograd.Function):
    @classmethod
    @abc.abstractmethod
    def call(cls, tangent_vector: torch.Tensor):
        pass

    @classmethod
    @abc.abstractmethod
    def forward(cls, ctx, tangent_vector, jacobians=None):
        pass


class LieGroupExpMap(torch.autograd.Function):
    @classmethod
    @abc.abstractmethod
    def call(
        cls,
        tangent_vector: torch.Tensor,
        jacobians: Optional[List[torch.Tensor]] = None,
    ):
        pass

    @classmethod
    @abc.abstractmethod
    def forward(cls, ctx, tangent_vector, jacobians=None):
        pass


class LieGroupInverse(torch.autograd.Function):
    @classmethod
    @abc.abstractmethod
    def call(
        cls,
        tangent_vector: torch.Tensor,
        jacobians: Optional[List[torch.Tensor]] = None,
    ):
        pass

    @classmethod
    def jacobian(cls, group: torch.Tensor) -> torch.Tensor:
        module = __import__(cls.__module__, fromlist=[""])
        if not module.check_group_tensor(group):
            raise ValueError(f"Invalid data tensor for {module.name()}")
        return -module.adjoint(group)

    @classmethod
    @abc.abstractmethod
    def forward(cls, ctx, tangent_vector, jacobians=None):
        pass
