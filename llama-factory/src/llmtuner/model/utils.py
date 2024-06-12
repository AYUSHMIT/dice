from enum import Enum, unique
from typing import TYPE_CHECKING, Dict, List

import torch
from transformers import PreTrainedModel
from transformers.integrations import is_deepspeed_zero3_enabled
from transformers.utils import cached_file
from transformers.utils.versions import require_version

from ..extras.constants import V_HEAD_SAFE_WEIGHTS_NAME, V_HEAD_WEIGHTS_NAME
from ..extras.logging import get_logger

if TYPE_CHECKING:
    from transformers import PretrainedConfig, PreTrainedTokenizer

    from ..hparams import ModelArguments

import json
import os

from safetensors.torch import load_file

logger = get_logger(__name__)


@unique
class QuantizationMethod(str, Enum):
    r"""
    Borrowed from `transformers.utils.quantization_config.QuantizationMethod`.
    """

    BITS_AND_BYTES = "bitsandbytes"
    GPTQ = "gptq"
    AWQ = "awq"
    AQLM = "aqlm"
    QUANTO = "quanto"


def add_z3_leaf_module(model: "PreTrainedModel", module: "torch.nn.Module") -> None:
    r"""
    Sets module as a leaf module to skip partitioning in deepspeed zero3.
    """
    if is_deepspeed_zero3_enabled():
        require_version("deepspeed>=0.13.0", "To fix: pip install deepspeed>=0.13.0")
        from deepspeed.utils import set_z3_leaf_modules  # type: ignore

        set_z3_leaf_modules(model, [module])


def find_all_linear_modules(model: "PreTrainedModel") -> List[str]:
    r"""
    Finds all available modules to apply lora or galore.
    """
    quantization_method = getattr(model, "quantization_method", None)
    if quantization_method is None:
        linear_cls = torch.nn.Linear
    elif quantization_method == QuantizationMethod.BITS_AND_BYTES:
        import bitsandbytes as bnb

        linear_cls = bnb.nn.Linear4bit if getattr(model, "is_loaded_in_4bit", False) else bnb.nn.Linear8bitLt
    else:
        raise ValueError("Finding linear modules for {} models is not supported.".format(quantization_method))

    output_layer_names = ["lm_head"]
    if model.config.model_type == "chatglm":
        output_layer_names.append("output_layer")
    elif model.config.model_type == "internlm2":
        output_layer_names.append("output")

    module_names = set()
    for name, module in model.named_modules():
        if isinstance(module, linear_cls) and not any(output_layer in name for output_layer in output_layer_names):
            module_names.add(name.split(".")[-1])

    logger.info("Found linear modules: {}".format(",".join(module_names)))
    return list(module_names)


def find_expanded_modules(model: "PreTrainedModel", target_modules: List[str], num_layer_trainable: int) -> List[str]:
    r"""
    Finds the modules in the expanded blocks to apply lora.
    """
    num_layers = getattr(model.config, "num_hidden_layers", None)
    if not num_layers:
        raise ValueError("Model was not supported.")

    if num_layers % num_layer_trainable != 0:
        raise ValueError(
            "`num_layers` {} should be divisible by `num_layer_trainable` {}.".format(num_layers, num_layer_trainable)
        )

    stride = num_layers // num_layer_trainable
    trainable_layer_ids = range(stride - 1, num_layers + stride - 1, stride)
    trainable_layers = [".{:d}.".format(idx) for idx in trainable_layer_ids]
    module_names = []
    for name, _ in model.named_modules():
        if any(target_module in name for target_module in target_modules) and any(
            trainable_layer in name for trainable_layer in trainable_layers
        ):
            module_names.append(name)

    logger.info("Apply lora to layers: {}".format(",".join(map(str, trainable_layer_ids))))
    return module_names


def load_valuehead_params(path_or_repo_id: str, model_args: "ModelArguments") -> Dict[str, torch.Tensor]:
    r"""
    Loads value head parameters from Hugging Face Hub or local disk.

    Returns: dict with keys `v_head.summary.weight` and `v_head.summary.bias`.
    """
    kwargs = {"path_or_repo_id": path_or_repo_id, "cache_dir": model_args.cache_dir, "token": model_args.hf_hub_token}

    try:
        from safetensors import safe_open

        vhead_file = cached_file(filename=V_HEAD_SAFE_WEIGHTS_NAME, **kwargs)
        with safe_open(vhead_file, framework="pt", device="cpu") as f:
            return {key: f.get_tensor(key) for key in f.keys()}
    except Exception as err:
        logger.info("Failed to load {}: {}".format(V_HEAD_SAFE_WEIGHTS_NAME, str(err)))

    try:
        vhead_file = cached_file(filename=V_HEAD_WEIGHTS_NAME, **kwargs)
        return torch.load(vhead_file, map_location="cpu")
    except Exception as err:
        logger.info("Failed to load {}: {}".format(V_HEAD_WEIGHTS_NAME, str(err)))

    logger.info("Provided path ({}) does not contain value head weights.".format(path_or_repo_id))
    logger.info("Ignore these messages if you are not resuming the training of a value head model.")
    return None


def register_autoclass(config: "PretrainedConfig", model: "PreTrainedModel", tokenizer: "PreTrainedTokenizer"):
    if "AutoConfig" in getattr(config, "auto_map", {}):
        config.__class__.register_for_auto_class()
    if "AutoModelForCausalLM" in getattr(config, "auto_map", {}):
        model.__class__.register_for_auto_class()
    if "AutoTokenizer" in tokenizer.init_kwargs.get("auto_map", {}):
        tokenizer.__class__.register_for_auto_class()


def sanity_check_keys_in_model_and_loaded(cur_stat_dict, loaded_keys: list):
    model_keys = cur_stat_dict.keys()
    loaded_keys
    missing_keys = [key for key in model_keys if key not in loaded_keys]
    unexpected_keys = [key for key in loaded_keys if key not in model_keys]
    if (len(missing_keys) > 0 or len(unexpected_keys) > 0):
        error_message = f"Error(s) in getting state_dict from loaded model."
        if len(missing_keys) > 0:
            str_missing_keys = ",".join([f'"{k}"' for k in missing_keys])
            error_message += f"\nMissing key(s): {str_missing_keys}."
        if len(unexpected_keys) > 0:
            str_unexpected_keys = ",".join([f'"{k}"' for k in unexpected_keys])
            error_message += f"\nMissing key(s): {str_unexpected_keys}."
        raise RuntimeError(error_message)


def get_safe_state_dict(cur_stat_dict, loaded_dir: str):
    with open(os.path.join(loaded_dir, "model.safetensors.index.json"), "r", encoding="utf-8") as f:
        index = json.load(f)

    shard_files = list(set(index["weight_map"].values()))
    stat_dict = {k: None for k in cur_stat_dict.keys()}
    loaded_keys = index["weight_map"].keys()
    sanity_check_keys_in_model_and_loaded(cur_stat_dict, loaded_keys)
    
    for shard_file in shard_files:
        shard_dict = load_file(os.path.join(loaded_dir, shard_file))
        for k, v in shard_dict.items():
            stat_dict[k] = v
    
    assert all([v is not None for v in stat_dict.values()]), "Some keys are not loaded."

    return stat_dict