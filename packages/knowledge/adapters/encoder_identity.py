"""Content identities for the loaded, process-local BGE document encoders."""

from __future__ import annotations

import hashlib
import json
import platform
from importlib.metadata import version
from typing import Any

from packages.core.canonical.json_hash import canonical_json_hash


def _class_name(obj: Any) -> str:
    return f"{type(obj).__module__}.{type(obj).__qualname__}"


def tokenizer_identity(tokenizer: Any) -> str:
    """Hash the live tokenizer, including its vocab, added tokens and normalizer."""
    if not tokenizer.is_fast or not _class_name(tokenizer).startswith("transformers."):
        raise ValueError("Unsupported tokenizer implementation")
    backend = json.loads(tokenizer.backend_tokenizer.to_str())
    if backend["model"].get("dropout"):
        raise ValueError("Stochastic tokenization")
    # Transformers overwrites these two backend fields for every call. Leaving
    # them in the hash would make it depend on the most recently encoded batch.
    backend.pop("truncation", None)
    backend.pop("padding", None)
    return canonical_json_hash(
        {
            "backend": backend,
            "class": _class_name(tokenizer),
            "max_length": tokenizer.model_max_length,
            "truncation_side": tokenizer.truncation_side,
            "padding_side": tokenizer.padding_side,
            "model_input_names": tokenizer.model_input_names,
            "special_tokens": tokenizer.special_tokens_map,
            "special_ids": tokenizer.all_special_ids,
            "split_special_tokens": tokenizer.split_special_tokens,
            "transformers": version("transformers"),
            "tokenizers": version("tokenizers"),
        }
    )


def dense_model_identity(model: Any) -> str:
    """Hash the effective weights once, independent of local directory or Hub alias."""
    import torch

    modules = []
    for module in model.children():
        if not _class_name(module).startswith("sentence_transformers.") or type(
            module
        ).__name__ not in {"Transformer", "Pooling", "Normalize"}:
            raise ValueError("Unrecognized sentence embedding module")
        modules.append((_class_name(module), module.get_config_dict()))
    backbone = model[0].auto_model
    if not _class_name(backbone).startswith("transformers.") or backbone.config.model_type not in {
        "bert",
        "xlm-roberta",
    }:
        raise ValueError("Unrecognized transformer backbone")
    weights = []
    for name, tensor in model.state_dict().items():
        raw = tensor.detach().contiguous().reshape(-1).view(torch.uint8)
        digest = hashlib.sha256()
        # Avoid copying the full vocabulary embedding from the GPU at once.
        for block in raw.split(8 * 1024 * 1024):
            digest.update(memoryview(block.cpu().numpy()))
        weights.append((name, list(tensor.shape), str(tensor.dtype), digest.hexdigest()))
    if not weights:
        raise ValueError("No model weights")
    config = backbone.config.to_dict()
    config.pop("_name_or_path", None)
    return canonical_json_hash(
        {
            "weights": sorted(weights),
            "backbone": _class_name(backbone),
            "config": config,
            "attention": backbone.config._attn_implementation,
            "modules": modules,
            "module_kwargs": model.module_kwargs,
            "tokenizer": tokenizer_identity(model.tokenizer),
            "max_seq_length": model.max_seq_length,
            "truncate_dim": model.truncate_dim,
            "prompts": model.prompts,
            "default_prompt_name": model.default_prompt_name,
            "sentence_transformers": version("sentence-transformers"),
            "numpy": version("numpy"),
            "torch": torch.__version__,
        }
    )


def inference_precision(device: Any) -> dict[str, Any]:
    import torch

    settings = {
        "device": device.type,
        "architecture": platform.machine(),
        "cpu_capability": torch.backends.cpu.get_cpu_capability(),
        "threads": torch.get_num_threads(),
        "matmul_precision": torch.get_float32_matmul_precision(),
        "autocast": torch.is_autocast_enabled(device.type),
        "autocast_dtype": str(torch.get_autocast_dtype(device.type)),
    }
    if device.type == "cuda":
        settings.update(
            gpu=torch.cuda.get_device_name(device),
            capability=torch.cuda.get_device_capability(device),
            cuda_version=torch.version.cuda,
            cudnn_version=torch.backends.cudnn.version(),
            flash_sdp=torch.backends.cuda.flash_sdp_enabled(),
            math_sdp=torch.backends.cuda.math_sdp_enabled(),
            mem_efficient_sdp=torch.backends.cuda.mem_efficient_sdp_enabled(),
            fp16_reduced_precision=torch.backends.cuda.matmul.allow_fp16_reduced_precision_reduction,
        )
    elif device.type != "cpu":
        raise ValueError("Unrecognized inference device")
    return settings
