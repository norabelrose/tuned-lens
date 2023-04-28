"""Provides lenses for decoding hidden states into logits."""
from copy import deepcopy
from dataclasses import dataclass, asdict
import inspect
from logging import warning
from pathlib import Path
import json
import abc

from tuned_lens.load_artifacts import load_lens_artifacts
from tuned_lens.nn.unembed import Unembed
from transformers import PreTrainedModel
from typing import Dict, Optional, Generator, Union
import torch as th


class Lens(abc.ABC, th.nn.Module):
    """Abstract base class for all Lens."""

    unembed: Unembed

    def __init__(self, unembed: Unembed):
        """Create a Lens.

        Args:
            unembed: The unembed operation to use.
        """
        super().__init__()

        self.unembed = unembed

    @abc.abstractmethod
    def transform_hidden(self, h: th.Tensor, idx: int) -> th.Tensor:
        """Convert a hidden state to the final hidden just before the unembeding.

        Args:
            h: The hidden state to convert.
            idx: The layer of the transformer these hidden states come from.
        """
        ...

    @abc.abstractmethod
    def forward(self, h: th.Tensor, idx: int) -> th.Tensor:
        """Decode hidden states into logits."""
        ...


class LogitLens(Lens):
    """Unembeds the residual stream into logits."""

    unembed: Unembed

    def __init__(
        self,
        unembed: Unembed,
    ):
        """Create a Logit Lens.

        Args:
            unembed: The unembed operation to use.
        """
        super().__init__(unembed)

    @classmethod
    def from_model(
        cls,
        model: PreTrainedModel,
    ) -> "LogitLens":
        """Create a LogitLens from a pretrained model.

        Args:
            model: A pretrained model from the transformers library you wish to inspect.
        """
        unembed = Unembed(model)
        return cls(unembed)

    def transform_hidden(self, h: th.Tensor, idx: int) -> th.Tensor:
        """For the LogitLens, this is the identity function."""
        del idx
        return h

    def forward(self, h: th.Tensor, idx: int) -> th.Tensor:
        """Decode a hidden state into logits.

        Args:
            h: The hidden state to decode.
            idx: the layer of the transformer these hidden states come from.
        """
        del idx
        return self.unembed.forward(h)


@dataclass
class TunedLensConfig:
    """A configuration for a TunedLens."""

    # The name of the base model this lens was tuned for.
    base_model_name_or_path: str
    # The hidden size of the base model.
    d_model: int
    # The number of layers in the base model.
    num_hidden_layers: int
    # whether to use a bias in the linear translators.
    bias: bool = True
    # The revision of the base model this lens was tuned for.
    base_model_revision: Optional[str] = None
    # The hash of the base's unembed model this lens was tuned for.
    unemebd_hash: Optional[str] = None
    # The name of the lens type.
    lens_type: str = "linear_tuned_lens"

    def to_dict(self):
        """Convert this config to a dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, config_dict: Dict):
        """Create a config from a dictionary."""
        config_dict = deepcopy(config_dict)
        # Drop unrecognized config keys
        unrecognized = set(config_dict) - set(inspect.getfullargspec(cls).args)
        for key in unrecognized:
            warning(f"Ignoring config key '{key}'")
            del config_dict[key]

        return cls(**config_dict)


class TunedLens(Lens):
    """A tuned lens for decoding hidden states into logits."""

    config: TunedLensConfig
    unembed: Unembed
    layer_translators: th.nn.ModuleList

    def __init__(
        self,
        unembed: Unembed,
        config: TunedLensConfig,
    ):
        """Create a TunedLens.

        Args:
            unembed: The unembed operation to use.
            config: The configuration for this lens.
        """
        super().__init__(unembed)

        self.config = config
        unembed_hash = unembed.unembedding_hash()
        config.unemebd_hash = unembed_hash

        translator = th.nn.Linear(config.d_model, config.d_model, bias=config.bias)
        translator.weight.data.zero_()
        translator.bias.data.zero_()

        # Don't include the final layer since it does not need a translator
        self.layer_translators = th.nn.ModuleList(
            [deepcopy(translator) for _ in range(self.config.num_hidden_layers)]
        )

    def __getitem__(self, item: int) -> th.nn.Module:
        """Get the probe module at the given index."""
        return self.layer_translators[item]

    def __iter__(self) -> Generator[th.nn.Module, None, None]:
        """Get iterator over the translators within the lens."""
        yield from self.layer_translators

    @classmethod
    def from_model(
        cls,
        model: PreTrainedModel,
        model_revision: Optional[str] = None,
        bias: bool = True,
    ) -> "TunedLens":
        """Create a lens from a pretrained model.

        Args:
            model: The model to create the lens from.
            model_revision: The git revision of the model to used.
            bias: Whether to use a bias in the linear translators.

        Returns:
            A TunedLens instance.
        """
        unembed = Unembed(model)
        config = TunedLensConfig(
            base_model_name_or_path=model.config.name_or_path,
            base_model_revision=model_revision,
            d_model=model.config.hidden_size,
            num_hidden_layers=model.config.num_hidden_layers,
            bias=bias,
        )

        return cls(unembed, config)

    @classmethod
    def from_model_and_pretrained(
        cls,
        model: PreTrainedModel,
        lens_resource_id: Optional[str] = None,
        **kwargs,
    ) -> "TunedLens":
        """Load a tuned lens from a folder or hugging face hub.

        Args:
            model: The model to create the lens from.
            lens_resource_id: The resource id of the lens to load. Defaults to the
                model's name_or_path.
            **kwargs: Additional arguments to pass to
                :func:`tuned_lens.load_artifacts.load_lens_artifacts` and
                `th.load <https://pytorch.org/docs/stable/generated/torch.load.html>`_.

        Returns:
            A TunedLens instance whose unembeding is derived from the given model
            and whose layer translators are loaded from the given resource id.
        """
        if lens_resource_id is None:
            lens_resource_id = model.config.name_or_path

        return cls.from_unembed_and_pretrained(
            Unembed(model), lens_resource_id, **kwargs
        )

    @classmethod
    def from_unembed_and_pretrained(
        cls,
        unembed: Unembed,
        lens_resource_id: str,
        **kwargs,
    ) -> "TunedLens":
        """Load a tuned lens from a folder or hugging face hub.

        Args:
            unembed: The unembed operation to use for the lens.
            lens_resource_id: The resource id of the lens to load.
            **kwargs: Additional arguments to pass to
                :func:`tuned_lens.load_artifacts.load_lens_artifacts` and
                `th.load <https://pytorch.org/docs/stable/generated/torch.load.html>`_.

        Returns:
            A TunedLens instance.
        """
        artifact_kwargs = set(inspect.getfullargspec(load_lens_artifacts).kwonlyargs)
        load_kwargs = set(inspect.getfullargspec(th.load).kwonlyargs)
        if unrecognized := [k not in (artifact_kwargs | load_kwargs) for k in kwargs]:
            raise ValueError(f"Unrecognized keyword argument(s) {unrecognized}.")

        # Create the config
        config_path, ckpt_path = load_lens_artifacts(
            lens_resource_id,
            **{k: v for k, v in kwargs.items() if k in artifact_kwargs},
        )

        with open(config_path, "r") as f:
            config = TunedLensConfig.from_dict(json.load(f))

        # validate the unembed is the same as the one used to train the lens
        if config.unemebd_hash and unembed.unembedding_hash() != config.unemebd_hash:
            warning(
                "The unembeding matrix hash does not match the lens' hash."
                "This lens may have been trained with a different unembeding."
            )

        # Create the lens
        lens = cls(unembed, config)

        # Load parameters
        state = th.load(
            ckpt_path, **{k: v for k, v in kwargs.items() if k in load_kwargs}
        )

        lens.layer_translators.load_state_dict(state)

        return lens

    def save(
        self,
        path: Union[Path, str],
        ckpt: str = "params.pt",
        config: str = "config.json",
    ) -> None:
        """Save the lens to a directory.

        Args:
            path : The path to the directory to save the lens to.
            ckpt : The name of the checkpoint file to save the parameters to.
            config : The name of the config file to save the config to.
        """
        path = Path(path)
        path.mkdir(exist_ok=True, parents=True)
        state_dict = self.layer_translators.state_dict()

        th.save(state_dict, path / ckpt)
        with open(path / config, "w") as f:
            json.dump(self.config.to_dict(), f)

    def normalize_(self):
        """Canonicalize the transforms by centering their weights and biases."""
        for linear in self:
            assert isinstance(linear, th.nn.Linear)

            A, b = linear.weight.data, linear.bias.data
            A -= A.mean(dim=0, keepdim=True)
            b -= b.mean()

    def transform_hidden(self, h: th.Tensor, idx: int) -> th.Tensor:
        """Transform hidden state from layer `idx`."""
        # Note that we add the translator output residually, in contrast to the formula
        # in the paper. By parametrizing it this way we ensure that weight decay
        # regularizes the transform toward the identity, not the zero transformation.
        return h + self[idx](h)

    def forward(self, h: th.Tensor, idx: int) -> th.Tensor:
        """Transform and then decode the hidden states into logits."""
        h = self.transform_hidden(h, idx)
        return self.unembed.forward(h)

    def __len__(self) -> int:
        """Return the number of layer translators in the lens."""
        return len(self.layer_translators)
