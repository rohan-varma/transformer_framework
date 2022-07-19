from pathlib import Path
from datetime import datetime
import torch
import time

from torch.distributed.fsdp import (
    FullyShardedDataParallel as FSDP,
    StateDictType,
    FullStateDictConfig,  # general model non-sharded, non-flattened params
    LocalStateDictConfig,  # flattened params, usable only by FSDP
    # ShardedStateDictConfig, # un-flattened param but shards, usable by other parallel schemes.
)

from torch.distributed._shard.checkpoint import (
    FileSystemReader,
    FileSystemWriter,
    save_state_dict,
    load_state_dict,
)


def get_date_of_run():
    """create date and time for file save uniqueness
    example: 2022-05-07-08:31:12_PM'
    """
    date_of_run = datetime.now().strftime("%Y-%m-%d-%I:%M:%S_%p")
    print(f"--> current date and time of run = {date_of_run}")
    return date_of_run


# create singleton saving policies to avoid making over and over
fullstate_save_policy = FullStateDictConfig(offload_to_cpu=True, rank0_only=True)


def save_model_checkpoint(
    model,
    optimizer,
    rank,
    cfg,
    epoch=1,
):
    """saving model via rank0 cpu streaming and full_state_dict"""

    # saving with rank0 cpu
    if not cfg.checkpoint_type == StateDictType.FULL_STATE_DICT:
        print(f" unable to handle checkpoint type {cfg.checkpoint_type}, aborting")

    with FSDP.state_dict_type(
        model, StateDictType.FULL_STATE_DICT, fullstate_save_policy
    ):
        cpu_state = model.state_dict()

    if cfg.verbose:
        print(f"saving process: rank {rank}  done w model state_dict\n")

    if rank == 0:
        print(f"--> saving model ...")
        # create save path
        save_dir = Path.cwd() / cfg.checkpoint_folder
        save_dir.mkdir(parents=True, exist_ok=True)
        save_name = cfg.model_save_name + "-" + str(epoch) + ".pt"
        save_full_path = str(save_dir) + "/" + save_name

        # save model
        torch.save(cpu_state, save_full_path)

        if cfg.verbose:
            print(f"model checkpoint saved for epoch {epoch} at {save_full_path}\n")


def load_model_checkpoint(model, rank, cfg, verbose=True):
    """load local checkpoint to rank0 cpu
    must be called * before * passing to FSDP"""

    if rank != 0:
        return

    # where is the checkpoint at...
    full_state_dict_model_path = (
        Path.cwd() / cfg.checkpoint_folder / cfg.checkpoint_model_filename
    )
    # is it present...
    if not full_state_dict_model_path.is_file():
        print(
            f"model checkpoint {full_state_dict_model_path} not present. Returning..."
        )
        return
    # load the checkpoint
    model_checkpoint = torch.load(full_state_dict_model_path)
    # integrate into loaded model
    model.load_state_dict(model_checkpoint)

    if cfg.verbose:
        print(f"model checkpoint loaded to rank0 cpu")


def save_optimizer_checkpoint(model, optimizer, rank, cfg, epoch=1):
    """save optimizer state via full state dict"""

    if cfg.verbose:
        print(f"--> optim state call on rank {rank}\n")

    # pull all sharded optimizer states to rank0 cpu...

    optim_state = FSDP.full_optim_state_dict(model, optimizer)

    if cfg.verbose:
        print(f"optim state dict ready on {rank} and len of {len(optim_state)}\n")

    if rank == 0:
        save_dir = Path.cwd() / cfg.checkpoint_folder
        save_dir.mkdir(parents=True, exist_ok=True)

        opt_save_name = (
            cfg.optimizer_name + "-" + cfg.model_save_name + "-" + str(epoch) + ".pt"
        )
        opt_save_full_path = save_dir / opt_save_name

        # note that saving can be time consuming...i.e. 1.5B can take up to 3 minutes (17GB)
        # thus always print state so no one thinks it has hung
        print(f"--> saving optimizer state...")

        torch.save(optim_state, opt_save_full_path)

        print(f"--> saved {opt_save_full_path} to disk")


def load_optimizer_checkpoint(model, optimizer, rank, cfg):
    """load an fdsp optimizer full_state checkpoint using scatter method
    this ensures only rank 0 loads the optimizer state dict and scatters to other ranks"""

    opt_file_path = Path.cwd() / cfg.checkpoint_folder / cfg.optimizer_checkpoint_file

    if not opt_file_path.is_file():
        print(
            f"warning - optimizer checkpoint not present {opt_file_path}. Returning. "
        )
        return

    full_osd = None

    if rank == 0:
        full_osd = torch.load(opt_file_path)

        if cfg.verbose:
            print(f"loaded full osd on rank 0")

    # called from all ranks, though only rank0 has a valid param for full_osd
    sharded_osd = FSDP.scatter_full_optim_state_dict(full_osd, model)

    if cfg.verbose:
        print(f"optimizer shard loaded on rank {rank}")

    # optimizer.load_state_dict(sharded_osd)
    # sharded_osd = FSDP.shard_full_optim_state_dict(full_osd, model)
    # optimizer.load_state_dict(sharded_osd)


def load_distributed_model_checkpoint(model, rank, cfg):

    if cfg.checkpoint_type == StateDictType.LOCAL_STATE_DICT:
        print(f"loading distributed checkpoint, rank {rank}...")
        folder_name = cfg.dist_checkpoint_root_folder+"/"+cfg.dist_checkpoint_folder+"-"+cfg.model_name

        checkdir = Path.cwd() / folder_name

        if not checkdir.exists():
            if rank==0:
                print(f"No checkpoint directory found...skipping")
            return

        if rank == 0:
            
            timer_start_load = time.perf_counter()

        reader = FileSystemReader(checkdir)

        with FSDP.state_dict_type(
            model,
            StateDictType.LOCAL_STATE_DICT,
        ):
            state_dict = model.state_dict()
            load_state_dict(state_dict, reader)
            model.load_state_dict(state_dict)

        print(f"--> local state loaded on rank {rank}")
        if rank == 0:

            timer_stop_loading = time.perf_counter()
            print(
                f"loading time for dist checkpoint = {timer_stop_loading-timer_start_load}"
            )

        return


def save_distributed_model_checkpoint(model, rank, cfg, epoch=1):
    # distributed checkpoint saving

    if rank == 0:
        print(f"Starting distributed checkpoint save...")
        save_time_start = time.perf_counter()

    # confirm type of checkpoint and save
    if cfg.checkpoint_type == StateDictType.LOCAL_STATE_DICT:
        # create writer to current path
        #folder_name = cfg.dist_checkpoint_folder+"-"+cfg.model_name
        folder_name = cfg.dist_checkpoint_root_folder+"/"+cfg.dist_checkpoint_folder+"-"+cfg.model_name
        save_dir = Path.cwd() / folder_name

        writer = FileSystemWriter(save_dir)

        with FSDP.state_dict_type(
            model,
            StateDictType.LOCAL_STATE_DICT,
        ):
            state_dict = model.state_dict()

        # write out distributed checkpoint
        save_state_dict(state_dict, writer)

        if rank == 0:
            save_time_stop = time.perf_counter()
            print(f"total save time = {save_time_stop - save_time_start}")
            print(f"--> distributed checkpoint saved at {save_dir}")

        return  
