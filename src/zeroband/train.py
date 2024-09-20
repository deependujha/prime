import os
from contextlib import nullcontext
import datetime
import logging  # Added logging import
from typing import Literal

import torch
from pydantic_config import parse_argv, BaseConfig
from torch.distributed import destroy_process_group, init_process_group
from einops import rearrange
from torch.nn import functional as F

from transformers import (
    AutoTokenizer,
    get_cosine_schedule_with_warmup,
)
from torch.distributed.fsdp import (
    FullyShardedDataParallel as FSDP,
    MixedPrecision,
)
from zeroband.utils import get_sharding_strategy
from zeroband.utils.monitor import WandbMonitor, DummyMonitor
from zeroband.data import TEST_VOCAB_SIZE, get_dataloader
from zeroband.models.llama import llama2_configs, llama3_configs, Transformer


### TODO

# use torch.idst.local rank instead of env var
# fix logger

local_rank = int(os.getenv("LOCAL_RANK", 0))

if local_rank == 0:
    log_level = os.getenv("ZERO_BAND_LOG_LEVEL", "INFO")
    logging.basicConfig(level=getattr(logging, log_level, logging.INFO))
else:
    logging.basicConfig(level=logging.CRITICAL)  # Disable logging for non-zero ranks

logger = logging.getLogger(__name__)

# Function to initialize the distributed process group
def ddp_setup():
    init_process_group()
    torch.cuda.set_device(int(os.environ["LOCAL_RANK"]))


class DilocoConfig(BaseConfig):
    outer_lr: float = 0.7
    inner_steps: int = 10


class DataConfig(BaseConfig):
    dataset_name_or_path: str = "allenai/c4"
    seq_length: int = 1024
    fake_data: bool = False
    num_workers: int = 4

class OptimConfig(BaseConfig):
    lr: float = 4e-4
    weight_decay: float = 0.1
    adam_betas1: float = 0.9
    adam_betas2: float = 0.95

    warmup_steps: int = 1000
    total_steps: int = 88_000
    batch_size: int = 512

class TrainConfig(BaseConfig):
    micro_bs: int
    torch_compile: bool = True
    sharding_strategy: str = "FULL_SHARD"


class Config(BaseConfig):

    # main config
    name_model: Literal["debugmodel", "150M", "271M", "1B", "7B", "13B", "26B", "70B"] = "150M"
    type_model: Literal["llama2","llama3"] = "llama2"

    project: str = "zeroband"
    metric_logger_type: Literal["wandb", "dummy"] = "wandb"
    

    # sub config
    diloco: DilocoConfig | None = None
    data: DataConfig = DataConfig()
    optim: OptimConfig = OptimConfig()
    train: TrainConfig
    


def get_model(name_model: str, type_model: str, tokenizer: AutoTokenizer) -> Transformer:
    """get the transformer model"""

    if type_model == "llama2":
        config = llama2_configs[name_model]
    elif type_model == "llama3":
        config = llama3_configs[name_model]
    else:
        raise ValueError(f"Model type {type_model} not supported")
    
    config.vocab_size = tokenizer.vocab_size if name_model != "debugmodel" else TEST_VOCAB_SIZE
    return Transformer(config)

def train(config: Config):
    sharding_strategy = get_sharding_strategy(config.train.sharding_strategy)
    local_rank = int(os.environ["LOCAL_RANK"])
    world_size = int(os.environ["WORLD_SIZE"])
    local_world_size = int(os.environ["LOCAL_WORLD_SIZE"])
    rank = int(os.environ["RANK"])

    # batch_size is the total batch size for all GPUs
    assert config.optim.batch_size % local_world_size == 0
    batch_size = config.optim.batch_size // local_world_size

    assert batch_size % config.train.micro_bs == 0
    gradient_accumulation_steps = batch_size // config.train.micro_bs

    tokenizer = AutoTokenizer.from_pretrained("mistralai/Mistral-7B-v0.1", use_fast=True)
    tokenizer.pad_token = "</s>"  # todo(sami): remove padding tokens once we have context stuffing

    logger.debug("tokenizer loaded")
    train_dataloader = get_dataloader(tokenizer.pad_token_id, world_size, rank, config.data.seq_length, config.train.micro_bs, config.data.num_workers)

    model = get_model(config.name_model, config.type_model, tokenizer=tokenizer)
    model = model.to(local_rank)
    logger.debug("model loaded")

    model = FSDP(
        model,
        sharding_strategy=sharding_strategy,
        mixed_precision=MixedPrecision(param_dtype=torch.bfloat16),
        use_orig_params=True,
    )

    if config.train.torch_compile:
        model = torch.compile(model)
    logger.debug("model compiled and fsdped")

    # Setup optimizers
    inner_optimizer = torch.optim.AdamW(model.parameters(), lr=config.optim.lr, weight_decay=config.optim.weight_decay, betas=(config.optim.adam_betas1, config.optim.adam_betas2))

    scheduler = get_cosine_schedule_with_warmup(
        inner_optimizer,
        num_warmup_steps=config.optim.warmup_steps,
        num_training_steps=config.optim.total_steps,
    )

    model.train()

    if rank == 0:
        logger_cls = WandbMonitor if config.metric_logger_type == "wandb" else DummyMonitor
        metric_logger = logger_cls(project=config.project, config=config.model_dump(), resume=False)

    train_dataloader_iterator = iter(train_dataloader)

    outer_step = 0
    num_inner_steps = config.diloco.inner_steps if config.diloco is not None else 1

    logger.info("starting training")
    while True:

        if num_inner_steps > 1:
            # if we don't use diloco we don't print the outer step logs
            logger.info(f"outer_step step: {outer_step}")

        for inner_step in range(num_inner_steps):
            loss_batch = 0

            for grad_acc_step in range(gradient_accumulation_steps):
                is_accumulating = grad_acc_step < gradient_accumulation_steps - 1
                batch = next(train_dataloader_iterator)
                input_ids = batch["input_ids"].to("cuda")
                labels = batch["labels"].to("cuda")

                with model.no_sync() if is_accumulating else nullcontext():
                    logits = model(tokens = input_ids).contiguous()
                    flatten_logits = rearrange(logits, "b seq vocab -> (b seq) vocab")
                    flatten_labels = rearrange(labels, "b seq -> (b seq)")

                    loss = F.cross_entropy(flatten_logits, flatten_labels, ignore_index=-100) / gradient_accumulation_steps
                    loss.backward()
                    loss_batch += loss.detach()

            model.clip_grad_norm_(1.0)  # gradient clipping
            inner_optimizer.step()
            scheduler.step()
            inner_optimizer.zero_grad()

            # logging
            real_step = outer_step * num_inner_steps + inner_step + 1 # add + 1 because inner_step start at 0
            inner_lr = [group["lr"] for group in inner_optimizer.param_groups][0]

            metrics = {
                "Loss": loss_batch.item(), # todo(sami): do local all reduce for the loss
                "step": real_step, 
                "inner_lr": inner_lr,
            }

            if rank == 0:
                metric_logger.log(metrics)

            logger.info(f"step: {real_step}, loss: {loss_batch.item()}, inner_lr: {inner_lr}")

        outer_step += 1

        if real_step >= config.optim.total_steps:
            # we only allow to break outisde of the inner loop.
            # This avoid ending the training in the middle of a the inner loop
            # Since ckpt strategy and all reduce is done at the outer loop level.
            break

    if rank == 0:
        metric_logger.finish()


if __name__ == "__main__":
    # Allow eager fallback during production so that that the training runs dont die
    # However, in development, we want to know that we broke torch compile
    torch._dynamo.config.suppress_errors = "ZERO_BAND_DEV" not in os.environ
    torch.set_float32_matmul_precision("high")
    ddp_setup()

    config = Config(**parse_argv())
    logger.debug(f"config: {config.model_dump()}")

    train(config)
    destroy_process_group()