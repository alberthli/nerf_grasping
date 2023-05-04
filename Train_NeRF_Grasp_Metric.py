# ---
# jupyter:
#   jupytext:
#     formats: ipynb,py:percent
#     text_representation:
#       extension: .py
#       format_name: percent
#       format_version: '1.3'
#       jupytext_version: 1.14.5
#   kernelspec:
#     display_name: Python 3 (ipykernel)
#     language: python
#     name: python3
# ---


# %%
def is_notebook() -> bool:
    try:
        shell = get_ipython().__class__.__name__
        if shell == "ZMQInteractiveShell":
            return True  # Jupyter notebook or qtconsole
        elif shell == "TerminalInteractiveShell":
            return False  # Terminal running IPython
        else:
            return False  # Other type (?)
    except NameError:
        return False  # Probably standard Python interpreter


# %% [markdown]
# # Imports

# %%
import math
import os
import pickle
import random
import shutil
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from enum import Enum, auto
from functools import partial
from typing import Any, Dict, List, Optional, Tuple

import h5py
import numpy as np
import plotly.graph_objects as go
import torch
import torch.nn as nn
from hydra import compose, initialize
from hydra.core.config_store import ConfigStore
from hydra.utils import instantiate
from localscope import localscope
from omegaconf import MISSING, OmegaConf
from sklearn.metrics import ConfusionMatrixDisplay, confusion_matrix, accuracy_score
from sklearn.utils.class_weight import compute_class_weight
from torch.utils.data import (
    DataLoader,
    Dataset,
    Subset,
    random_split,
    SubsetRandomSampler,
)
from torchinfo import summary
from torchviz import make_dot
from wandb.util import generate_id
from torch.profiler import profile, record_function, ProfilerActivity


import wandb

# %%
if is_notebook():
    from tqdm.notebook import tqdm as std_tqdm
else:
    from tqdm import tqdm as std_tqdm

tqdm = partial(std_tqdm, dynamic_ncols=True)


# %% [markdown]
# # Setup Config for Static Type-Checking


# %%
OmegaConf.register_new_resolver("eval", eval, replace=True)
OmegaConf.register_new_resolver(
    "datetime_str", lambda: datetime.now().strftime("%Y-%m-%d_%H-%M-%S"), replace=True
)


# %%
@dataclass
class WandbConfig:
    entity: str = MISSING
    project: str = MISSING
    name: str = MISSING
    group: str = MISSING
    job_type: str = MISSING


class PreprocessDensityType(Enum):
    DENSITY = auto()
    ALPHA = auto()
    WEIGHT = auto()


@dataclass
class DataConfig:
    frac_val: float = MISSING
    frac_test: float = MISSING
    frac_train: float = MISSING

    input_dataset_root_dir: str = MISSING
    input_dataset_path: str = MISSING
    max_num_data_points: Optional[int] = MISSING


@dataclass
class DataLoaderConfig:
    batch_size: int = MISSING
    num_workers: int = MISSING
    pin_memory: bool = MISSING

    load_nerf_grid_inputs_in_ram: bool = MISSING
    load_grasp_successes_in_ram: bool = MISSING


@dataclass
class PreprocessConfig:
    flip_left_right_randomly: bool = MISSING
    density_type: PreprocessDensityType = MISSING
    add_invariance_transformations: bool = MISSING
    rotate_polar_angle: bool = MISSING
    reflect_around_xz_plane_randomly: bool = MISSING
    remove_y_axis: bool = MISSING


@dataclass
class TrainingConfig:
    grad_clip_val: float = MISSING
    lr: float = MISSING
    n_epochs: int = MISSING
    log_grad_freq: int = MISSING
    log_grad_on_epoch_0: bool = MISSING
    val_freq: int = MISSING
    val_on_epoch_0: bool = MISSING
    save_checkpoint_freq: int = MISSING
    save_checkpoint_on_epoch_0: bool = MISSING
    confusion_matrix_freq: int = MISSING
    save_confusion_matrix_on_epoch_0: bool = MISSING
    use_dataloader_subset: bool = MISSING


class ConvOutputTo1D(Enum):
    FLATTEN = auto()  # (N, C, H, W) -> (N, C*H*W)
    AVG_POOL_SPATIAL = auto()  # (N, C, H, W) -> (N, C, 1, 1) -> (N, C)
    AVG_POOL_CHANNEL = auto()  # (N, C, H, W) -> (N, 1, H, W) -> (N, H*W)
    MAX_POOL_SPATIAL = auto()  # (N, C, H, W) -> (N, C, 1, 1) -> (N, C)
    MAX_POOL_CHANNEL = auto()  # (N, C, H, W) -> (N, 1, H, W) -> (N, H*W)


class PoolType(Enum):
    MAX = auto()
    AVG = auto()


@dataclass
class NeuralNetworkConfig:
    conv_channels: List[int] = MISSING
    pool_type: PoolType = MISSING
    dropout_prob: float = MISSING
    conv_output_to_1d: ConvOutputTo1D = MISSING
    mlp_hidden_layers: List[int] = MISSING


@dataclass
class CheckpointWorkspaceConfig:
    root_dir: str = MISSING
    leaf_dir: str = MISSING
    force_no_resume: bool = MISSING


@dataclass
class Config:
    data: DataConfig = MISSING
    dataloader: DataLoaderConfig = MISSING
    preprocess: PreprocessConfig = MISSING
    wandb: WandbConfig = MISSING
    training: TrainingConfig = MISSING
    neural_network: NeuralNetworkConfig = MISSING
    checkpoint_workspace: CheckpointWorkspaceConfig = MISSING
    random_seed: int = MISSING
    visualize_data: bool = MISSING
    dry_run: bool = MISSING


# %%
config_store = ConfigStore.instance()
config_store.store(name="config", node=Config)


# %% [markdown]
# # Load Config

# %%
if is_notebook():
    arguments = []
else:
    arguments = sys.argv[1:]
    print(f"arguments = {arguments}")


# %%
from hydra.errors import ConfigCompositionException

try:
    with initialize(version_base="1.1", config_path="Train_NeRF_Grasp_Metric_cfg"):
        raw_cfg = compose(config_name="config", overrides=arguments)

    # Runtime type-checking
    cfg: Config = instantiate(raw_cfg)
except ConfigCompositionException as e:
    print(f"ConfigCompositionException: {e}")
    print()
    print(f"e.__cause__ = {e.__cause__}")
    raise e.__cause__

# %%
print(f"Config:\n{OmegaConf.to_yaml(cfg)}")

# %%
if cfg.dry_run:
    print("Dry run passed. Exiting.")
    exit()

# %% [markdown]
# # Set Random Seed


# %%
@localscope.mfc
def set_seed(seed):
    random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    np.random.seed(seed)
    torch.set_num_threads(1)  # TODO: Is this slowing things down?

    # torch.backends.cudnn.deterministic = True
    # torch.backends.cudnn.benchmark = False
    os.environ["PYTHONHASHSEED"] = str(seed)
    print(f"Set random seed to {seed}")


set_seed(cfg.random_seed)

# %% [markdown]
# # Setup Checkpoint Workspace and Maybe Resume Previous Run


# %%
@localscope.mfc
def load_checkpoint(checkpoint_workspace_dir_path: str) -> Optional[Dict[str, Any]]:
    checkpoint_filepaths = sorted(
        [
            os.path.join(checkpoint_workspace_dir_path, filename)
            for filename in os.listdir(checkpoint_workspace_dir_path)
            if filename.endswith(".pt")
        ]
    )
    if len(checkpoint_filepaths) == 0:
        print("No checkpoint found")
        return None
    return torch.load(checkpoint_filepaths[-1])


# %%
# Set up checkpoint_workspace
if not os.path.exists(cfg.checkpoint_workspace.root_dir):
    os.makedirs(cfg.checkpoint_workspace.root_dir)

checkpoint_workspace_dir_path = os.path.join(
    cfg.checkpoint_workspace.root_dir, cfg.checkpoint_workspace.leaf_dir
)

# Remove checkpoint_workspace directory if force_no_resume is set
if (
    os.path.exists(checkpoint_workspace_dir_path)
    and cfg.checkpoint_workspace.force_no_resume
):
    print(f"force_no_resume = {cfg.checkpoint_workspace.force_no_resume}")
    print(f"Removing checkpoint_workspace directory at {checkpoint_workspace_dir_path}")
    shutil.rmtree(checkpoint_workspace_dir_path)
    print("Done removing checkpoint_workspace directory")

# Read wandb_run_id from checkpoint_workspace if it exists
wandb_run_id_filepath = os.path.join(checkpoint_workspace_dir_path, "wandb_run_id.txt")
if os.path.exists(checkpoint_workspace_dir_path):
    print(
        f"checkpoint_workspace directory already exists at {checkpoint_workspace_dir_path}"
    )

    print(f"Loading wandb_run_id from {wandb_run_id_filepath}")
    with open(wandb_run_id_filepath, "r") as f:
        wandb_run_id = f.read()
    print(f"Done loading wandb_run_id = {wandb_run_id}")

else:
    print(f"Creating checkpoint_workspace directory at {checkpoint_workspace_dir_path}")
    os.makedirs(checkpoint_workspace_dir_path)
    print("Done creating checkpoint_workspace directory")

    wandb_run_id = generate_id()
    print(f"Saving wandb_run_id = {wandb_run_id} to {wandb_run_id_filepath}")
    with open(wandb_run_id_filepath, "w") as f:
        f.write(wandb_run_id)
    print("Done saving wandb_run_id")

# %% [markdown]
# # Setup Wandb Logging

# %%
# Add to config
wandb.init(
    entity=cfg.wandb.entity,
    project=cfg.wandb.project,
    name=cfg.wandb.name,
    group=cfg.wandb.group if len(cfg.wandb.group) > 0 else None,
    job_type=cfg.wandb.job_type if len(cfg.wandb.job_type) > 0 else None,
    config=OmegaConf.to_container(cfg, throw_on_missing=True),
    id=wandb_run_id,
    resume="never" if cfg.checkpoint_workspace.force_no_resume else "allow",
    reinit=True,
)

# %% [markdown]
# # Dataset and Dataloader

# %%
# CONSTANTS AND PARAMS
NUM_PTS_X, NUM_PTS_Y, NUM_PTS_Z = 83, 21, 37
NUM_XYZ = 3
NUM_DENSITY = 1
NUM_CHANNELS = NUM_XYZ + NUM_DENSITY
INPUT_EXAMPLE_SHAPE = (NUM_CHANNELS, NUM_PTS_X, NUM_PTS_Y, NUM_PTS_Z)
NERF_COORDINATE_START_IDX, NERF_COORDINATE_END_IDX = 0, 3
NERF_DENSITY_START_IDX, NERF_DENSITY_END_IDX = 3, 4
DELTA = 0.001  # 1mm between grid points

assert NERF_COORDINATE_END_IDX == NERF_COORDINATE_START_IDX + NUM_XYZ
assert NERF_DENSITY_END_IDX == NERF_DENSITY_START_IDX + NUM_DENSITY


# %%


class NeRFGrid_To_GraspSuccess_HDF5_Dataset(Dataset):
    # @localscope.mfc  # ValueError: Cell is empty
    def __init__(
        self,
        input_hdf5_filepath: str,
        max_num_data_points: Optional[int] = None,
        load_nerf_grid_inputs_in_ram: bool = False,
        load_grasp_successes_in_ram: bool = False,
    ):
        super().__init__()
        self.input_hdf5_filepath = input_hdf5_filepath

        # Recommended in https://discuss.pytorch.org/t/dataloader-when-num-worker-0-there-is-bug/25643/13
        self.hdf5_file = None

        with h5py.File(self.input_hdf5_filepath, "r") as hdf5_file:
            self.len = self._set_length(
                hdf5_file=hdf5_file, max_num_data_points=max_num_data_points
            )

            # Check that the data is in the expected format
            assert len(hdf5_file["/grasp_success"].shape) == 1
            assert hdf5_file["/nerf_grid_input"].shape[1:] == INPUT_EXAMPLE_SHAPE

            # This is usually too big for RAM
            self.nerf_grid_inputs = (
                torch.from_numpy(hdf5_file["/nerf_grid_input"][()]).float()
                if load_nerf_grid_inputs_in_ram
                else None
            )

            # This is small enough to fit in RAM
            self.grasp_successes = (
                torch.from_numpy(hdf5_file["/grasp_success"][()]).long()
                if load_grasp_successes_in_ram
                else None
            )

    @localscope.mfc
    def _set_length(self, hdf5_file: h5py.File, max_num_data_points: Optional[int]):
        length = (
            hdf5_file.attrs["num_data_points"]
            if "num_data_points" in hdf5_file.attrs
            else hdf5_file["/grasp_success"].shape[0]
        )
        if length != hdf5_file["/grasp_success"].shape[0]:
            print(
                f"WARNING: num_data_points = {length} != grasp_success.shape[0] = {hdf5_file['/grasp_success'].shape[0]}"
            )

        # Constrain length of dataset if max_num_data_points is set
        if max_num_data_points is not None:
            print(f"Constraining dataset length to {max_num_data_points}")
            length = max_num_data_points

        return length

    @localscope.mfc
    def __len__(self):
        return self.len

    @localscope.mfc(
        allowed=[
            "INPUT_EXAMPLE_SHAPE",
            "NERF_DENSITY_START_IDX",
            "NERF_DENSITY_END_IDX",
        ]
    )
    def __getitem__(self, idx: int):
        if self.hdf5_file is None:
            # Hope to speed up with rdcc params
            self.hdf5_file = h5py.File(
                self.input_hdf5_filepath,
                "r",
                rdcc_nbytes=1024**2 * 4_000,
                rdcc_w0=0.75,
                rdcc_nslots=4_000,
            )

        nerf_grid_input = (
            torch.from_numpy(self.hdf5_file["/nerf_grid_input"][idx]).float()
            if self.nerf_grid_inputs is None
            else self.nerf_grid_inputs[idx]
        )

        grasp_success = (
            torch.from_numpy(np.array(self.hdf5_file["/grasp_success"][idx])).long()
            if self.grasp_successes is None
            else self.grasp_successes[idx]
        )

        assert nerf_grid_input.shape == INPUT_EXAMPLE_SHAPE
        assert grasp_success.shape == ()

        return nerf_grid_input, grasp_success


# %%
@localscope.mfc(
    allowed=[
        "ctx_factory",  # global from torch.no_grad
        "DELTA",
    ]
)
@torch.no_grad()
def preprocess_to_alpha(nerf_densities: torch.Tensor):
    # alpha = 1 - exp(-delta * sigma)
    #       = probability of collision within this segment starting from beginning of segment
    return 1.0 - torch.exp(-DELTA * nerf_densities)


@localscope.mfc(
    allowed=[
        "ctx_factory",  # global from torch.no_grad
        "INPUT_EXAMPLE_SHAPE",
        "NERF_DENSITY_START_IDX",
        "NERF_DENSITY_END_IDX",
        "NUM_PTS_X",
    ]
)
@torch.no_grad()
def preprocess_to_weight(nerf_densities: torch.Tensor):
    # alpha_j = 1 - exp(-delta_j * sigma_j)
    #         = probability of collision within this segment starting from beginning of segment
    # weight_j = alpha_j * (1 - alpha_{j-1}) * ... * (1 - alpha_1))
    #          = probability of collision within j-th segment starting from left edge

    @localscope.mfc
    def compute_weight(alpha: torch.Tensor):
        # [1 - alpha_1, (1 - alpha_1) * (1 - alpha_2), ..., (1 - alpha_1) * ... * (1 - alpha_{NUM_PTS_X}))]
        cumprod_1_minus_alpha_from_left = (1 - alpha).cumprod(dim=x_axis_dim)

        # [1, 1 - alpha_1, (1 - alpha_1) * (1 - alpha_2), ..., (1 - alpha_1) * ... * (1 - alpha_{NUM_PTS_X-1}))]
        cumprod_1_minus_alpha_from_left_shifted = torch.cat(
            [
                torch.ones_like(
                    cumprod_1_minus_alpha_from_left[:, :1],
                    dtype=alpha.dtype,
                    device=alpha.device,
                ),
                cumprod_1_minus_alpha_from_left[:, :-1],
            ],
            dim=x_axis_dim,
        )

        # weight_j = alpha_j * (1 - alpha_{j-1}) * ... * (1 - alpha_1))
        weight = alpha * cumprod_1_minus_alpha_from_left_shifted
        return weight

    assert nerf_densities.shape[-3:] == (NUM_PTS_X, NUM_PTS_Y, NUM_PTS_Z)
    x_axis_dim = -3

    # [alpha_1, alpha_2, ..., alpha_{NUM_PTS_X}]
    alpha = preprocess_to_alpha(nerf_densities)

    # weight_j = alpha_j * (1 - alpha_{j-1}) * ... * (1 - alpha_1))
    weight = compute_weight(alpha)

    return weight


# %%
@localscope.mfc(
    allowed=[
        "cfg",
        "NUM_PTS_X",
        "NUM_PTS_Y",
        "NUM_PTS_Z",
        "NUM_XYZ",
        "NUM_CHANNELS",
        "NERF_DENSITY_START_IDX",
        "NERF_DENSITY_END_IDX",
        "NERF_COORDINATE_START_IDX",
        "NERF_COORDINATE_END_IDX",
    ]
)
def get_nerf_densities_and_points(nerf_grid_inputs: torch.Tensor):
    batch_size = nerf_grid_inputs.shape[0]
    assert (
        len(nerf_grid_inputs.shape) == 5
        and nerf_grid_inputs.shape[0] == batch_size
        and nerf_grid_inputs.shape[1] == NUM_CHANNELS
        and nerf_grid_inputs.shape[2] in [NUM_PTS_X // 2, NUM_PTS_X]
        and nerf_grid_inputs.shape[3] == NUM_PTS_Y
        and nerf_grid_inputs.shape[4] == NUM_PTS_Z
    )

    assert torch.is_tensor(nerf_grid_inputs)

    nerf_densities = nerf_grid_inputs[
        :, NERF_DENSITY_START_IDX:NERF_DENSITY_END_IDX
    ].squeeze(dim=1)
    nerf_points = nerf_grid_inputs[:, NERF_COORDINATE_START_IDX:NERF_COORDINATE_END_IDX]

    return nerf_densities, nerf_points


@localscope.mfc(
    allowed=[
        "cfg",
        "NUM_PTS_X",
        "NUM_PTS_Y",
        "NUM_PTS_Z",
        "NUM_XYZ",
    ]
)
def get_global_params(nerf_points: torch.Tensor):
    batch_size = nerf_points.shape[0]
    assert (
        len(nerf_points.shape) == 5
        and nerf_points.shape[0] == batch_size
        and nerf_points.shape[1] == NUM_XYZ
        and nerf_points.shape[2] in [NUM_PTS_X // 2, NUM_PTS_X]
        and nerf_points.shape[3] == NUM_PTS_Y
        and nerf_points.shape[4] == NUM_PTS_Z
    )
    assert torch.is_tensor(nerf_points)

    new_origin_x_idx, new_origin_y_idx, new_origin_z_idx = (
        0,
        NUM_PTS_Y // 2,
        NUM_PTS_Z // 2,
    )

    new_origin = nerf_points[:, :, new_origin_x_idx, new_origin_y_idx, new_origin_z_idx]
    assert new_origin.shape == (batch_size, NUM_XYZ)

    new_x_axis = nn.functional.normalize(
        nerf_points[:, :, new_origin_x_idx + 1, new_origin_y_idx, new_origin_z_idx]
        - new_origin,
        dim=-1,
    )
    new_y_axis = nn.functional.normalize(
        nerf_points[:, :, new_origin_x_idx, new_origin_y_idx + 1, new_origin_z_idx]
        - new_origin,
        dim=-1,
    )
    new_z_axis = nn.functional.normalize(
        nerf_points[:, :, new_origin_x_idx, new_origin_y_idx, new_origin_z_idx + 1]
        - new_origin,
        dim=-1,
    )

    assert torch.isclose(
        torch.cross(new_x_axis, new_y_axis, dim=-1), new_z_axis, rtol=1e-3, atol=1e-3
    ).all()

    # new_z_axis is implicit from the cross product of new_x_axis and new_y_axis
    return new_origin, new_x_axis, new_y_axis


@localscope.mfc(
    allowed=[
        "cfg",
        "NUM_XYZ",
    ]
)
def invariance_transformation(
    left_global_params: Tuple[torch.Tensor, ...],
    right_global_params: Tuple[torch.Tensor, ...],
    rotate_polar_angle: bool = False,
    reflect_around_xz_plane_randomly: bool = False,
    remove_y_axis: bool = False,
):
    left_origin, left_x_axis, left_y_axis = left_global_params
    right_origin, right_x_axis, right_y_axis = right_global_params

    batch_size = left_origin.shape[0]

    assert (
        left_origin.shape
        == right_origin.shape
        == left_x_axis.shape
        == right_x_axis.shape
        == left_y_axis.shape
        == right_y_axis.shape
        == (batch_size, NUM_XYZ)
    )

    # Always do rotation wrt left
    # Get azimuth angle of left_origin
    azimuth_angle = torch.atan2(left_origin[:, 1], left_origin[:, 0])

    # Reverse azimuth angle around z to get back to xz plane (left_origin_y = 0)
    # This handles invariance in both xy and yaw (angle around z)
    transformation_matrix_around_z = torch.tensor(
        [
            [torch.cos(-azimuth_angle), -torch.sin(-azimuth_angle), 0, 0],
            [torch.sin(-azimuth_angle), torch.cos(-azimuth_angle), 0, 0],
            [0, 0, 1, 0],
            [0, 0, 0, 1],
        ]
    )

    @localscope.mfc(allowed=["batch_size"])
    def transform(transformation_matrix, point):
        assert transformation_matrix.shape == (4, 4)
        assert point.shape == (batch_size, 3)

        transformed_point = transformation_matrix @ torch.cat(
            [point, torch.tensor([1.0])]
        )

        return transformed_point[:, :3]

    # Transform all
    left_origin = transform(transformation_matrix_around_z, left_origin)
    left_x_axis = transform(transformation_matrix_around_z, left_x_axis)
    left_y_axis = transform(transformation_matrix_around_z, left_y_axis)
    right_origin = transform(transformation_matrix_around_z, right_origin)
    right_x_axis = transform(transformation_matrix_around_z, right_x_axis)
    right_y_axis = transform(transformation_matrix_around_z, right_y_axis)

    assert torch.all(
        torch.isclose(left_origin[:, 1], torch.zeros_like(left_origin[:, 1]))
    )  # left_origin_y = 0

    if rotate_polar_angle:
        # Always do rotation wrt left
        # Get polar angle of left_origin
        polar_angle = torch.atan2(
            torch.sqrt(left_origin[0] ** 2 + left_origin[1] ** 2), left_origin[2]
        )

        # Angle between x axis and left_origin
        polar_angle = torch.pi / 2 - polar_angle

        # Rotation around y, positive to bring down to z = 0
        transformation_matrix_around_y = torch.tensor(
            [
                [torch.cos(polar_angle), 0, torch.sin(polar_angle), 0],
                [0, 1, 0, 0],
                [-torch.sin(polar_angle), 0, torch.cos(polar_angle), 0],
                [0, 0, 0, 1],
            ]
        )

        # Transform all
        left_origin = transform(transformation_matrix_around_y, left_origin)
        left_x_axis = transform(transformation_matrix_around_y, left_x_axis)
        left_y_axis = transform(transformation_matrix_around_y, left_y_axis)
        right_origin = transform(transformation_matrix_around_y, right_origin)
        right_x_axis = transform(transformation_matrix_around_y, right_x_axis)
        right_y_axis = transform(transformation_matrix_around_y, right_y_axis)

        assert torch.all(
            torch.isclose(left_origin[:, 2], torch.zeros_like(left_origin[:, 2]))
        )  # left_origin_z = 0

    # To handle additional invariance, we can reflect around planes of symmetry
    # xy plane probably doesn't make sense, as gravity affects this axis
    # yz is handled already by the rotation around z
    # xz plane is probably the best choice, as there is symmetry around moving left and right

    # Reflect around xz plane
    if reflect_around_xz_plane_randomly:
        reflect = torch.rand((batch_size,)) > 0.5
        left_origin = torch.where(
            reflect[:, None], left_origin * torch.tensor([1, -1, 1]), left_origin
        )
        left_x_axis = torch.where(
            reflect[:, None], left_x_axis * torch.tensor([1, -1, 1]), left_x_axis
        )
        left_y_axis = torch.where(
            reflect[:, None], left_y_axis * torch.tensor([1, -1, 1]), left_y_axis
        )
        right_origin = torch.where(
            reflect[:, None], right_origin * torch.tensor([1, -1, 1]), right_origin
        )
        right_x_axis = torch.where(
            reflect[:, None], right_x_axis * torch.tensor([1, -1, 1]), right_x_axis
        )
        right_y_axis = torch.where(
            reflect[:, None], right_y_axis * torch.tensor([1, -1, 1]), right_y_axis
        )

    # y-axis gives you the orientation around the approach direction, which may not be important
    if remove_y_axis:
        return left_origin, left_x_axis, right_origin, right_x_axis
    return (
        left_origin,
        left_x_axis,
        left_y_axis,
        right_origin,
        right_x_axis,
        right_y_axis,
    )


@localscope.mfc(allowed=["cfg", "INPUT_EXAMPLE_SHAPE", "NUM_PTS_X", "NUM_XYZ"])
def preprocess_nerf_grid_inputs(
    nerf_grid_inputs: torch.Tensor,
    flip_left_right_randomly: bool = False,
    preprocess_density_type: PreprocessDensityType = PreprocessDensityType.DENSITY,
    add_invariance_transformations: bool = False,
    rotate_polar_angle: bool = False,
    reflect_around_xz_plane_randomly: bool = False,
    remove_y_axis: bool = False,
):
    batch_size = nerf_grid_inputs.shape[0]
    assert nerf_grid_inputs.shape == (
        batch_size,
        *INPUT_EXAMPLE_SHAPE,
    )
    assert torch.is_tensor(nerf_grid_inputs)

    # Split into left and right
    # Need to rotate the right side around the z-axis, so that the x-axis is pointing toward the left
    # so need to flip the x and y axes
    num_pts_per_side = NUM_PTS_X // 2
    x_dim, y_dim = -3, -2
    left_nerf_grid_inputs = nerf_grid_inputs[:, :, :num_pts_per_side, :, :]
    right_nerf_grid_inputs = nerf_grid_inputs[:, :, -num_pts_per_side:, :, :].flip(
        dims=(x_dim, y_dim)
    )

    left_nerf_densities, left_nerf_points = get_nerf_densities_and_points(
        left_nerf_grid_inputs
    )
    right_nerf_densities, right_nerf_points = get_nerf_densities_and_points(
        right_nerf_grid_inputs
    )

    # Flip which side is left and right
    if flip_left_right_randomly:
        flip = torch.rand((batch_size,)) > 0.5
        left_nerf_densities, right_nerf_densities = (
            torch.where(
                flip[:, None, None, None], right_nerf_densities, left_nerf_densities
            ),
            torch.where(
                flip[:, None, None, None], left_nerf_densities, right_nerf_densities
            ),
        )
        left_nerf_points, right_nerf_points = (
            torch.where(
                flip[:, None, None, None, None], right_nerf_points, left_nerf_points
            ),
            torch.where(
                flip[:, None, None, None, None], left_nerf_points, right_nerf_points
            ),
        )

    # Extract global params
    left_global_params = get_global_params(left_nerf_points)
    right_global_params = get_global_params(right_nerf_points)

    # Preprocess densities
    preprocess_type_to_fn = {
        PreprocessDensityType.DENSITY: lambda x: x,
        PreprocessDensityType.ALPHA: preprocess_to_alpha,
        PreprocessDensityType.WEIGHT: preprocess_to_weight,
    }
    preprocess_density_fn = preprocess_type_to_fn[preprocess_density_type]
    left_nerf_densities = preprocess_density_fn(left_nerf_densities)
    right_nerf_densities = preprocess_density_fn(right_nerf_densities)

    # Invariance transformations
    if add_invariance_transformations:
        left_global_params, right_global_params = invariance_transformation(
            left_global_params=left_global_params,
            right_global_params=right_global_params,
            rotate_polar_angle=rotate_polar_angle,
            reflect_around_xz_plane_randomly=reflect_around_xz_plane_randomly,
            remove_y_axis=remove_y_axis,
        )

    # Concatenate global params into a single tensor
    assert len(left_global_params) == len(right_global_params)
    assert all([param.shape == (batch_size, NUM_XYZ) for param in left_global_params])
    assert all([param.shape == (batch_size, NUM_XYZ) for param in right_global_params])
    left_global_params = torch.cat(left_global_params, dim=1)
    right_global_params = torch.cat(right_global_params, dim=1)

    return [
        (left_nerf_densities, left_global_params),
        (right_nerf_densities, right_global_params),
    ]


# %%
class DatasetType(Enum):
    HDF5_FILE = auto()


assert cfg.data.input_dataset_path.endswith(".h5")
dataset_type = DatasetType.HDF5_FILE
input_dataset_full_path = os.path.join(
    cfg.data.input_dataset_root_dir, cfg.data.input_dataset_path
)

if dataset_type == DatasetType.HDF5_FILE:
    full_dataset = NeRFGrid_To_GraspSuccess_HDF5_Dataset(
        input_dataset_full_path,
        max_num_data_points=cfg.data.max_num_data_points,
        load_nerf_grid_inputs_in_ram=cfg.dataloader.load_nerf_grid_inputs_in_ram,
        load_grasp_successes_in_ram=cfg.dataloader.load_grasp_successes_in_ram,
    )
else:
    raise ValueError(f"Unknown dataset type: {dataset_type}")

train_dataset, val_dataset, test_dataset = random_split(
    full_dataset,
    [cfg.data.frac_train, cfg.data.frac_val, cfg.data.frac_test],
    generator=torch.Generator().manual_seed(cfg.random_seed),
)

# %%
print(f"Train dataset size: {len(train_dataset)}")
print(f"Val dataset size: {len(val_dataset)}")
print(f"Test dataset size: {len(test_dataset)}")

# %%
assert len(set.intersection(set(train_dataset.indices), set(val_dataset.indices))) == 0
assert len(set.intersection(set(train_dataset.indices), set(test_dataset.indices))) == 0
assert len(set.intersection(set(val_dataset.indices), set(test_dataset.indices))) == 0


# %%
@localscope.mfc(allowed=["cfg"])
def custom_collate_fn(batch):
    batch = torch.utils.data.dataloader.default_collate(batch)

    nerf_grid_inputs, grasp_successes = batch
    (
        (left_nerf_densities, left_global_params),
        (right_nerf_densities, right_global_params),
    ) = preprocess_nerf_grid_inputs(
        nerf_grid_inputs=nerf_grid_inputs,
        flip_left_right_randomly=cfg.preprocess.flip_left_right_randomly,
        preprocess_density_type=cfg.preprocess.density_type,
        add_invariance_transformations=cfg.preprocess.add_invariance_transformations,
        rotate_polar_angle=cfg.preprocess.rotate_polar_angle,
        reflect_around_xz_plane_randomly=cfg.preprocess.reflect_around_xz_plane_randomly,
        remove_y_axis=cfg.preprocess.remove_y_axis,
    )

    return (
        (
            (left_nerf_densities, left_global_params),
            (right_nerf_densities, right_global_params),
        ),
        grasp_successes,
    )


# %%
train_loader = DataLoader(
    train_dataset,
    batch_size=cfg.dataloader.batch_size,
    shuffle=True,
    pin_memory=cfg.dataloader.pin_memory,
    num_workers=cfg.dataloader.num_workers,
    collate_fn=custom_collate_fn,
)
val_loader = DataLoader(
    val_dataset,
    batch_size=cfg.dataloader.batch_size,
    shuffle=False,
    pin_memory=cfg.dataloader.pin_memory,
    num_workers=cfg.dataloader.num_workers,
    collate_fn=custom_collate_fn,
)
test_loader = DataLoader(
    test_dataset,
    batch_size=cfg.dataloader.batch_size,
    shuffle=False,
    pin_memory=cfg.dataloader.pin_memory,
    num_workers=cfg.dataloader.num_workers,
    collate_fn=custom_collate_fn,
)

# %%
# TODO REMOVE
(
    (left_nerf_densities, left_global_params),
    (right_nerf_densities, right_global_params),
), grasp_successes = next(iter(val_loader))
print("DONE")

# %%
left_nerf_densities.shape, left_global_params.shape, right_nerf_densities.shape, right_global_params.shape, grasp_successes.shape

# %%
# Visualize nerf densities
import matplotlib.pyplot as plt
idx = 0
left_nerf_density = left_nerf_densities[idx].cpu().numpy().transpose(0, 2, 1)  # Tranpose because z is last, but should be height
num_imgs, height, width = left_nerf_density.shape
images = [left_nerf_density[i] for i in range(num_imgs)]
num_rows = math.ceil(math.sqrt(num_imgs))
num_cols = math.ceil(num_imgs / num_rows)

fig, axes = plt.subplots(num_rows, num_cols, figsize=(20, 20))
for i, ax in enumerate(axes.flatten()):
    ax.axis("off")

    if i >= num_imgs:
        continue

    ax.imshow(images[i])
    ax.set_title(f"Image {i}")

fig.suptitle("Left Nerf Densities")
fig.tight_layout()
wandb.log({"Left Nerf Densities": fig})

# %%
# Visualize nerf densities
import matplotlib.pyplot as plt
idx = 0
right_nerf_density = right_nerf_densities[idx].cpu().numpy().transpose(0, 2, 1)  # Tranpose because z is last, but should be height
num_imgs, height, width = right_nerf_density.shape
images = [right_nerf_density[i] for i in range(num_imgs)]
num_rows = math.ceil(math.sqrt(num_imgs))
num_cols = math.ceil(num_imgs / num_rows)

fig, axes = plt.subplots(num_rows, num_cols, figsize=(20, 20))
for i, ax in enumerate(axes.flatten()):
    ax.axis("off")

    if i >= num_imgs:
        continue

    ax.imshow(images[i])
    ax.set_title(f"Image {i}")

fig.suptitle("Right Nerf Densities")
fig.tight_layout()
wandb.log({"Right Nerf Densities": fig})

# %%
# need to be in shape (num_imgs, 3, H, W) and be np.uint8 in [0, 255]
wandb_video = (np.array(images).reshape(num_imgs, 1, height, width).repeat(repeats=3, axis=1) * 255).astype(np.uint8)
wandb.log({"Left Nerf Densities Video": wandb.Video(wandb_video, fps=4, format="mp4")})

# %%
# need to be in shape (num_imgs, 3, H, W) and be np.uint8 in [0, 255]
wandb_video = (np.array(images).reshape(num_imgs, 1, height, width).repeat(repeats=3, axis=1) * 255).astype(np.uint8)
wandb.log({"Right Nerf Densities Video": wandb.Video(wandb_video, fps=4, format="mp4")})


# %%
# Create 1D visualization
left_max_density = left_nerf_density.max(axis=(1, 2))
right_nerf_density = right_nerf_densities[idx].cpu().numpy().transpose(0, 2, 1)  # Tranpose because z is last, but should be height
right_max_density = right_nerf_density.max(axis=(1, 2))
max_density = np.concatenate([left_max_density, right_max_density[::-1]])
plt.plot(range(len(left_max_density)), left_max_density)
plt.plot(range(len(right_max_density)), right_max_density)
plt.title("Max Alpha")
plt.xlabel(f"Idx (Left = 0, Right = {len(max_density) - 1})")
plt.ylabel("Max Alpha")

# %%
create_datapoint_plotly_fig(
    dataset=val_dataset, datapoint_name=Phase.VAL.name.lower(), save_to_wandb=True
)
# %%
val_dataset.indices[0]

# %%
create_detailed_plot_with_mesh(
    full_dataset=full_dataset, idx_to_visualize=1174768, save_to_wandb=True
)


# %%
# TODO: END REMOVE

# %%
print(f"Train loader size: {len(train_loader)}")
print(f"Val loader size: {len(val_loader)}")
print(f"Test loader size: {len(test_loader)}")

# %%
assert math.ceil(len(train_dataset) / cfg.dataloader.batch_size) == len(train_loader)
assert math.ceil(len(val_dataset) / cfg.dataloader.batch_size) == len(val_loader)
assert math.ceil(len(test_dataset) / cfg.dataloader.batch_size) == len(test_loader)

# %% [markdown]
# # Visualize Datapoint

# %%


class Phase(Enum):
    TRAIN = auto()
    VAL = auto()
    TEST = auto()


# %%
@localscope.mfc
def wandb_log_plotly_fig(plotly_fig: go.Figure, title: str, group_name: str = "plotly"):
    if wandb.run is None:
        print("Not logging plotly fig to wandb because wandb.run is None")
        return

    path_to_plotly_html = f"{wandb.run.dir}/{title}.html"
    print(f"Saving to {path_to_plotly_html}")

    plotly_fig.write_html(path_to_plotly_html)
    wandb_table = wandb.Table(columns=[title])
    wandb_table.add_data(wandb.Html(path_to_plotly_html))
    if group_name is not None:
        wandb.log({f"{group_name}/{title}": wandb_table})
    else:
        wandb.log({title: wandb_table})
    print(f"Successfully logged {title} to wandb")


# %%
@localscope.mfc
def get_isaac_origin_lines():
    x_line_np = np.array([[0.0, 0.0, 0.0], [0.1, 0.0, 0.0]])
    y_line_np = np.array([[0.0, 0.0, 0.0], [0.0, 0.1, 0.0]])
    z_line_np = np.array([[0.0, 0.0, 0.0], [0.0, 0.0, 0.1]])

    lines = []
    for line_np, name, color in [
        (x_line_np, "X", "red"),
        (y_line_np, "Y", "green"),
        (z_line_np, "Z", "blue"),
    ]:
        lines.append(
            go.Scatter3d(
                x=line_np[:, 0],
                y=line_np[:, 1],
                z=line_np[:, 2],
                mode="lines",
                line=dict(width=2, color=color),
                name=f"Isaac Origin {name} Axis",
            )
        )
    return lines


@localscope.mfc(allowed=["NUM_XYZ"])
def get_colored_points_scatter(points: torch.Tensor, colors: torch.Tensor):
    assert len(points.shape) == 2 and points.shape[1] == NUM_XYZ
    assert len(colors.shape) == 1

    # Use plotly to make scatter3d plot
    scatter = go.Scatter3d(
        x=points[:, 0],
        y=points[:, 1],
        z=points[:, 2],
        mode="markers",
        marker=dict(
            size=5,
            color=colors,
            colorscale="viridis",
            colorbar=dict(title="Density Scale"),
        ),
        name="Query Point Densities",
    )

    return scatter


# %%
@localscope.mfc(
    allowed=[
        "INPUT_EXAMPLE_SHAPE",
        "NUM_DENSITY",
        "NUM_PTS_X",
        "NUM_PTS_Y",
        "NUM_PTS_Z",
        "NERF_DENSITY_START_IDX",
        "NERF_DENSITY_END_IDX",
        "NUM_XYZ",
        "NERF_COORDINATE_START_IDX",
        "NERF_COORDINATE_END_IDX",
    ]
)
def create_datapoint_plotly_fig(
    dataset: NeRFGrid_To_GraspSuccess_HDF5_Dataset,
    datapoint_name: str,
    idx_to_visualize: int = 0,
    save_to_wandb: bool = False,
) -> go.Figure:
    nerf_grid_input, grasp_success = dataset[idx_to_visualize]

    assert nerf_grid_input.shape == INPUT_EXAMPLE_SHAPE
    assert len(np.array(grasp_success).shape) == 0

    nerf_densities = nerf_grid_input[
        NERF_DENSITY_START_IDX:NERF_DENSITY_END_IDX, :, :, :
    ]
    assert nerf_densities.shape == (NUM_DENSITY, NUM_PTS_X, NUM_PTS_Y, NUM_PTS_Z)

    nerf_points = nerf_grid_input[
        NERF_COORDINATE_START_IDX:NERF_COORDINATE_END_IDX
    ].permute(1, 2, 3, 0)
    assert nerf_points.shape == (NUM_PTS_X, NUM_PTS_Y, NUM_PTS_Z, NUM_XYZ)

    isaac_origin_lines = get_isaac_origin_lines()
    colored_points_scatter = get_colored_points_scatter(
        nerf_points.reshape(-1, NUM_XYZ), nerf_densities.reshape(-1)
    )

    layout = go.Layout(
        scene=dict(xaxis=dict(title="X"), yaxis=dict(title="Y"), zaxis=dict(title="Z")),
        showlegend=True,
        title=f"{datapoint_name} datapoint: success={grasp_success}",
        width=800,
        height=800,
    )

    # Create the figure
    fig = go.Figure(layout=layout)
    for line in isaac_origin_lines:
        fig.add_trace(line)
    fig.add_trace(colored_points_scatter)
    fig.update_layout(legend_orientation="h")

    if save_to_wandb:
        wandb_log_plotly_fig(plotly_fig=fig, title=f"{datapoint_name}_datapoint")
    return fig


# %%
if cfg.visualize_data:
    create_datapoint_plotly_fig(
        dataset=train_dataset,
        datapoint_name=Phase.TRAIN.name.lower(),
        save_to_wandb=True,
    )

# %%
if cfg.visualize_data:
    create_datapoint_plotly_fig(
        dataset=val_dataset, datapoint_name=Phase.VAL.name.lower(), save_to_wandb=True
    )


# %%
@localscope.mfc
def create_plotly_mesh(obj_filepath, scale=1.0, offset=None, color="lightpink"):
    if offset is None:
        offset = np.zeros(3)

    # Read in the OBJ file
    with open(obj_filepath, "r") as f:
        lines = f.readlines()

    # Extract the vertex coordinates and faces from the OBJ file
    vertices = []
    faces = []
    for line in lines:
        if line.startswith("v "):
            vertex = [float(i) * scale for i in line.split()[1:4]]
            vertices.append(vertex)
        elif line.startswith("f "):
            face = [int(i.split("/")[0]) - 1 for i in line.split()[1:4]]
            faces.append(face)

    # Convert the vertex coordinates and faces to numpy arrays
    vertices = np.array(vertices)
    faces = np.array(faces)

    assert len(vertices.shape) == 2 and vertices.shape[1] == 3
    assert len(faces.shape) == 2 and faces.shape[1] == 3

    vertices += offset.reshape(1, 3)

    # Create the mesh3d trace
    mesh = go.Mesh3d(
        x=vertices[:, 0],
        y=vertices[:, 1],
        z=vertices[:, 2],
        i=faces[:, 0],
        j=faces[:, 1],
        k=faces[:, 2],
        color=color,
        opacity=0.5,
        name=f"Mesh: {os.path.basename(obj_filepath)}",
    )

    return mesh


# %%
@localscope.mfc
def create_detailed_plot_with_mesh(
    full_dataset: NeRFGrid_To_GraspSuccess_HDF5_Dataset,
    idx_to_visualize: int = 0,
    save_to_wandb: bool = False,
):
    # Hacky function that reads from both the input dataset and the acronym dataset
    # To create a detailed plot with the mesh and the grasp
    fig = create_datapoint_plotly_fig(
        dataset=full_dataset,
        datapoint_name="full data",
        save_to_wandb=False,
        idx_to_visualize=idx_to_visualize,
    )

    ACRONYM_ROOT_DIR = "/juno/u/tylerlum/github_repos/acronym/data/grasps"
    MESH_ROOT_DIR = "assets/objects"
    LEFT_TIP_POSITION_GRASP_FRAME = np.array(
        [4.10000000e-02, -7.27595772e-12, 1.12169998e-01]
    )
    RIGHT_TIP_POSITION_GRASP_FRAME = np.array(
        [-4.10000000e-02, -7.27595772e-12, 1.12169998e-01]
    )

    # Get acronym filename and grasp transform from input dataset
    with h5py.File(full_dataset.input_hdf5_filepath, "r") as hdf5_file:
        acronym_filename = hdf5_file["/acronym_filename"][idx_to_visualize].decode(
            "utf-8"
        )
        grasp_transform = np.array(hdf5_file["/grasp_transform"][idx_to_visualize])

    # Get mesh info from acronym dataset
    acronym_filepath = os.path.join(ACRONYM_ROOT_DIR, acronym_filename)
    with h5py.File(acronym_filepath, "r") as acronym_hdf5_file:
        mesh_filename = acronym_hdf5_file["object/file"][()].decode("utf-8")
        mesh_filepath = os.path.join(MESH_ROOT_DIR, mesh_filename)

        import trimesh

        mesh = trimesh.load(mesh_filepath, force="mesh")
        mesh_scale = float(acronym_hdf5_file["object/scale"][()])
        mesh_centroid = np.array(mesh.centroid) * mesh_scale

    left_tip = (
        np.matmul(
            grasp_transform, np.concatenate([LEFT_TIP_POSITION_GRASP_FRAME, [1.0]])
        )[:3]
        - mesh_centroid
    )
    right_tip = (
        np.matmul(
            grasp_transform, np.concatenate([RIGHT_TIP_POSITION_GRASP_FRAME, [1.0]])
        )[:3]
        - mesh_centroid
    )

    # Draw mesh, ensure -centroid offset so that mesh centroid is centered at origin
    fig.add_trace(
        create_plotly_mesh(
            obj_filepath=mesh_filepath,
            scale=mesh_scale,
            offset=-mesh_centroid,
            color="lightpink",
        )
    )

    # Draw line from left_tip to right_tip
    fig.add_trace(
        go.Scatter3d(
            x=[left_tip[0], right_tip[0]],
            y=[left_tip[1], right_tip[1]],
            z=[left_tip[2], right_tip[2]],
            mode="lines",
            line=dict(color="red", width=10),
            name="Grasp (should align with query points)",
        )
    )

    if save_to_wandb:
        wandb_log_plotly_fig(
            plotly_fig=fig, title=f"Detailed Mesh Plot idx={idx_to_visualize}"
        )

    return fig


# %%
if cfg.visualize_data:
    create_datapoint_plotly_fig(
        dataset=full_dataset,
        datapoint_name="full",
        idx_to_visualize=0,
        save_to_wandb=True,
    )

# %%
if cfg.visualize_data:
    create_detailed_plot_with_mesh(
        full_dataset=full_dataset, idx_to_visualize=0, save_to_wandb=True
    )

# %%

# TODO: END OF NEW

# %% [markdown]
# # Visualize Dataset Distribution


# %%
@localscope.mfc
def create_grasp_success_distribution_fig(
    train_dataset: Subset, input_dataset_full_path: str, save_to_wandb: bool = False
):
    try:
        with h5py.File(input_dataset_full_path, "r") as hdf5_file:
            grasp_successes_np = np.array(
                hdf5_file["/grasp_success"][
                    sorted(train_dataset.indices)
                ]  # Must be ascending
            )

        # Plot histogram in plotly
        fig = go.Figure(
            data=[
                go.Histogram(
                    x=grasp_successes_np,
                    name="Grasp Successes",
                    marker_color="blue",
                ),
            ],
            layout=go.Layout(
                title="Distribution of Grasp Successes",
                xaxis=dict(title="Grasp Success"),
                yaxis=dict(title="Frequency"),
            ),
        )
        if save_to_wandb:
            wandb_log_plotly_fig(
                plotly_fig=fig, title="Distribution of Grasp Successes"
            )
        return fig

    except Exception as e:
        print(f"Error: {e}")
        print("Skipping visualization of grasp success distribution")


if cfg.visualize_data:
    create_grasp_success_distribution_fig(
        train_dataset=train_dataset,
        input_dataset_full_path=input_dataset_full_path,
        save_to_wandb=True,
    )


# %%
@localscope.mfc(
    allowed=[
        "INPUT_EXAMPLE_SHAPE",
        "NERF_COORDINATE_START_IDX",
        "NERF_COORDINATE_END_IDX",
        "NERF_DENSITY_START_IDX",
        "NERF_DENSITY_END_IDX",
        "tqdm",
    ]
)
def create_nerf_grid_input_distribution_figs(
    train_loader: DataLoader, save_to_wandb: bool = False
):
    nerf_coordinate_mins, nerf_coordinate_means, nerf_coordinate_maxs = [], [], []
    nerf_density_mins, nerf_density_means, nerf_density_maxs = [], [], []
    for nerf_grid_inputs, _ in tqdm(
        train_loader, desc="Calculating nerf_grid_inputs dataset statistics"
    ):
        assert nerf_grid_inputs.shape[1:] == INPUT_EXAMPLE_SHAPE
        nerf_coordinates = nerf_grid_inputs[
            :, NERF_COORDINATE_START_IDX:NERF_COORDINATE_END_IDX
        ]
        nerf_densities = nerf_grid_inputs[
            :, NERF_DENSITY_START_IDX:NERF_DENSITY_END_IDX
        ]

        nerf_coordinate_mins.append(nerf_coordinates.min().item())
        nerf_coordinate_means.append(nerf_coordinates.mean().item())
        nerf_coordinate_maxs.append(nerf_coordinates.max().item())

        nerf_density_mins.append(nerf_densities.min().item())
        nerf_density_means.append(nerf_densities.mean().item())
        nerf_density_maxs.append(nerf_densities.max().item())

    nerf_coordinate_mins, nerf_coordinate_means, nerf_coordinate_maxs = (
        np.array(nerf_coordinate_mins),
        np.array(nerf_coordinate_means),
        np.array(nerf_coordinate_maxs),
    )
    nerf_coordinate_min = nerf_coordinate_mins.min()
    nerf_coordinate_mean = nerf_coordinate_means.mean()
    nerf_coordinate_max = nerf_coordinate_maxs.max()
    print(f"nerf_coordinate_min: {nerf_coordinate_min}")
    print(f"nerf_coordinate_mean: {nerf_coordinate_mean}")
    print(f"nerf_coordinate_max: {nerf_coordinate_max}")

    nerf_density_mins, nerf_density_means, nerf_density_maxs = (
        np.array(nerf_density_mins),
        np.array(nerf_density_means),
        np.array(nerf_density_maxs),
    )
    nerf_density_min = nerf_density_mins.min()
    nerf_density_mean = nerf_density_means.mean()
    nerf_density_max = nerf_density_maxs.max()
    print(f"nerf_density_min: {nerf_density_min}")
    print(f"nerf_density_mean: {nerf_density_mean}")
    print(f"nerf_density_max: {nerf_density_max}")

    # Coordinates
    coordinates_fig = go.Figure(
        data=[
            go.Histogram(
                x=nerf_coordinate_mins,
                name="Min",
                marker_color="blue",
            ),
            go.Histogram(
                x=nerf_coordinate_means,
                name="Mean",
                marker_color="orange",
            ),
            go.Histogram(
                x=nerf_coordinate_maxs,
                name="Max",
                marker_color="green",
            ),
        ],
        layout=go.Layout(
            title="Distribution of nerf_coordinates (Aggregated to Fit in RAM)",
            xaxis=dict(title="nerf_coordinates"),
            yaxis=dict(title="Frequency"),
            barmode="overlay",
        ),
    )

    # Density
    density_fig = go.Figure(
        data=[
            go.Histogram(
                x=nerf_density_mins,
                name="Min",
                marker_color="blue",
            ),
            go.Histogram(
                x=nerf_density_means,
                name="Mean",
                marker_color="orange",
            ),
            go.Histogram(
                x=nerf_density_maxs,
                name="Max",
                marker_color="green",
            ),

# %%
# need to be in shape (num_imgs, 3, H, W) and be np.uint8 in [0, 255]
wandb_video = (np.array(images).reshape(num_imgs, 1, height, width).repeat(repeats=3, axis=1) * 255).astype(np.uint8)
wandb.log({"Left Nerf Densities Video": wandb.Video(wandb_video, fps=4, format="mp4")})

# %%
# Create 1D visualization
left_max_density = left_nerf_density.max(axis=(1, 2))
right_nerf_density = right_nerf_densities[idx].cpu().numpy().transpose(0, 2, 1)  # Tranpose because z is last, but should be height
right_max_density = right_nerf_density.max(axis=(1, 2))
max_density = np.concatenate([left_max_density, right_max_density[::-1]])
plt.plot(max_density)
plt.title("Max Alpha")
plt.xlabel(f"Idx (Left = 0, Right = {len(max_density) - 1})")
plt.ylabel("Max Alpha")

# %%
create_datapoint_plotly_fig(
    dataset=val_dataset, datapoint_name=Phase.VAL.name.lower(), save_to_wandb=True
)
# %%
val_dataset.indices[0]

# %%
create_detailed_plot_with_mesh(
    full_dataset=full_dataset, idx_to_visualize=1174768, save_to_wandb=True
)


# %%
# TODO: END REMOVE

# %%
print(f"Train loader size: {len(train_loader)}")
print(f"Val loader size: {len(val_loader)}")
print(f"Test loader size: {len(test_loader)}")

# %%
assert math.ceil(len(train_dataset) / cfg.dataloader.batch_size) == len(train_loader)
assert math.ceil(len(val_dataset) / cfg.dataloader.batch_size) == len(val_loader)
assert math.ceil(len(test_dataset) / cfg.dataloader.batch_size) == len(test_loader)

# %% [markdown]
# # Visualize Datapoint

# %%


class Phase(Enum):
    TRAIN = auto()
    VAL = auto()
    TEST = auto()


# %%
@localscope.mfc
def wandb_log_plotly_fig(plotly_fig: go.Figure, title: str, group_name: str = "plotly"):
    if wandb.run is None:
        print("Not logging plotly fig to wandb because wandb.run is None")
        return

    path_to_plotly_html = f"{wandb.run.dir}/{title}.html"
    print(f"Saving to {path_to_plotly_html}")

    plotly_fig.write_html(path_to_plotly_html)
    wandb_table = wandb.Table(columns=[title])
    wandb_table.add_data(wandb.Html(path_to_plotly_html))
    if group_name is not None:
        wandb.log({f"{group_name}/{title}": wandb_table})
    else:
        wandb.log({title: wandb_table})
    print(f"Successfully logged {title} to wandb")


# %%
@localscope.mfc
def get_isaac_origin_lines():
    x_line_np = np.array([[0.0, 0.0, 0.0], [0.1, 0.0, 0.0]])
    y_line_np = np.array([[0.0, 0.0, 0.0], [0.0, 0.1, 0.0]])
    z_line_np = np.array([[0.0, 0.0, 0.0], [0.0, 0.0, 0.1]])

    lines = []
    for line_np, name, color in [
        (x_line_np, "X", "red"),
        (y_line_np, "Y", "green"),
        (z_line_np, "Z", "blue"),
    ]:
        lines.append(
            go.Scatter3d(
                x=line_np[:, 0],
                y=line_np[:, 1],
                z=line_np[:, 2],
                mode="lines",
                line=dict(width=2, color=color),
                name=f"Isaac Origin {name} Axis",
            )
        )
    return lines


@localscope.mfc(allowed=["NUM_XYZ"])
def get_colored_points_scatter(points: torch.Tensor, colors: torch.Tensor):
    assert len(points.shape) == 2 and points.shape[1] == NUM_XYZ
    assert len(colors.shape) == 1

    # Use plotly to make scatter3d plot
    scatter = go.Scatter3d(
        x=points[:, 0],
        y=points[:, 1],
        z=points[:, 2],
        mode="markers",
        marker=dict(
            size=5,
            color=colors,
            colorscale="viridis",
            colorbar=dict(title="Density Scale"),
        ),
        name="Query Point Densities",
    )

    return scatter


# %%
@localscope.mfc(
    allowed=[
        "INPUT_EXAMPLE_SHAPE",
        "NUM_DENSITY",
        "NUM_PTS_X",
        "NUM_PTS_Y",
        "NUM_PTS_Z",
        "NERF_DENSITY_START_IDX",
        "NERF_DENSITY_END_IDX",
        "NUM_XYZ",
        "NERF_COORDINATE_START_IDX",
        "NERF_COORDINATE_END_IDX",
    ]
)
def create_datapoint_plotly_fig(
    dataset: NeRFGrid_To_GraspSuccess_HDF5_Dataset,
    datapoint_name: str,
    idx_to_visualize: int = 0,
    save_to_wandb: bool = False,
) -> go.Figure:
    nerf_grid_input, grasp_success = dataset[idx_to_visualize]

    assert nerf_grid_input.shape == INPUT_EXAMPLE_SHAPE
    assert len(np.array(grasp_success).shape) == 0

    nerf_densities = nerf_grid_input[
        NERF_DENSITY_START_IDX:NERF_DENSITY_END_IDX, :, :, :
    ]
    assert nerf_densities.shape == (NUM_DENSITY, NUM_PTS_X, NUM_PTS_Y, NUM_PTS_Z)

    nerf_points = nerf_grid_input[
        NERF_COORDINATE_START_IDX:NERF_COORDINATE_END_IDX
    ].permute(1, 2, 3, 0)
    assert nerf_points.shape == (NUM_PTS_X, NUM_PTS_Y, NUM_PTS_Z, NUM_XYZ)

    isaac_origin_lines = get_isaac_origin_lines()
    colored_points_scatter = get_colored_points_scatter(
        nerf_points.reshape(-1, NUM_XYZ), nerf_densities.reshape(-1)
    )

    layout = go.Layout(
        scene=dict(xaxis=dict(title="X"), yaxis=dict(title="Y"), zaxis=dict(title="Z")),
        showlegend=True,
        title=f"{datapoint_name} datapoint: success={grasp_success}",
        width=800,
        height=800,
    )

    # Create the figure
    fig = go.Figure(layout=layout)
    for line in isaac_origin_lines:
        fig.add_trace(line)
    fig.add_trace(colored_points_scatter)
    fig.update_layout(legend_orientation="h")

    if save_to_wandb:
        wandb_log_plotly_fig(plotly_fig=fig, title=f"{datapoint_name}_datapoint")
    return fig


# %%
if cfg.visualize_data:
    create_datapoint_plotly_fig(
        dataset=train_dataset,
        datapoint_name=Phase.TRAIN.name.lower(),
        save_to_wandb=True,
    )

# %%
if cfg.visualize_data:
    create_datapoint_plotly_fig(
        dataset=val_dataset, datapoint_name=Phase.VAL.name.lower(), save_to_wandb=True
    )


# %%
@localscope.mfc
def create_plotly_mesh(obj_filepath, scale=1.0, offset=None, color="lightpink"):
    if offset is None:
        offset = np.zeros(3)

    # Read in the OBJ file
    with open(obj_filepath, "r") as f:
        lines = f.readlines()

    # Extract the vertex coordinates and faces from the OBJ file
    vertices = []
    faces = []
    for line in lines:
        if line.startswith("v "):
            vertex = [float(i) * scale for i in line.split()[1:4]]
            vertices.append(vertex)
        elif line.startswith("f "):
            face = [int(i.split("/")[0]) - 1 for i in line.split()[1:4]]
            faces.append(face)

    # Convert the vertex coordinates and faces to numpy arrays
    vertices = np.array(vertices)
    faces = np.array(faces)

    assert len(vertices.shape) == 2 and vertices.shape[1] == 3
    assert len(faces.shape) == 2 and faces.shape[1] == 3

    vertices += offset.reshape(1, 3)

    # Create the mesh3d trace
    mesh = go.Mesh3d(
        x=vertices[:, 0],
        y=vertices[:, 1],
        z=vertices[:, 2],
        i=faces[:, 0],
        j=faces[:, 1],
        k=faces[:, 2],
        color=color,
        opacity=0.5,
        name=f"Mesh: {os.path.basename(obj_filepath)}",
    )

    return mesh


# %%
@localscope.mfc
def create_detailed_plot_with_mesh(
    full_dataset: NeRFGrid_To_GraspSuccess_HDF5_Dataset,
    idx_to_visualize: int = 0,
    save_to_wandb: bool = False,
):
    # Hacky function that reads from both the input dataset and the acronym dataset
    # To create a detailed plot with the mesh and the grasp
    fig = create_datapoint_plotly_fig(
        dataset=full_dataset,
        datapoint_name="full data",
        save_to_wandb=False,
        idx_to_visualize=idx_to_visualize,
    )

    ACRONYM_ROOT_DIR = "/juno/u/tylerlum/github_repos/acronym/data/grasps"
    MESH_ROOT_DIR = "assets/objects"
    LEFT_TIP_POSITION_GRASP_FRAME = np.array(
        [4.10000000e-02, -7.27595772e-12, 1.12169998e-01]
    )
    RIGHT_TIP_POSITION_GRASP_FRAME = np.array(
        [-4.10000000e-02, -7.27595772e-12, 1.12169998e-01]
    )

    # Get acronym filename and grasp transform from input dataset
    with h5py.File(full_dataset.input_hdf5_filepath, "r") as hdf5_file:
        acronym_filename = hdf5_file["/acronym_filename"][idx_to_visualize].decode(
            "utf-8"
        )
        grasp_transform = np.array(hdf5_file["/grasp_transform"][idx_to_visualize])

    # Get mesh info from acronym dataset
    acronym_filepath = os.path.join(ACRONYM_ROOT_DIR, acronym_filename)
    with h5py.File(acronym_filepath, "r") as acronym_hdf5_file:
        mesh_filename = acronym_hdf5_file["object/file"][()].decode("utf-8")
        mesh_filepath = os.path.join(MESH_ROOT_DIR, mesh_filename)

        import trimesh

        mesh = trimesh.load(mesh_filepath, force="mesh")
        mesh_scale = float(acronym_hdf5_file["object/scale"][()])
        mesh_centroid = np.array(mesh.centroid) * mesh_scale

    left_tip = (
        np.matmul(
            grasp_transform, np.concatenate([LEFT_TIP_POSITION_GRASP_FRAME, [1.0]])
        )[:3]
        - mesh_centroid
    )
    right_tip = (
        np.matmul(
            grasp_transform, np.concatenate([RIGHT_TIP_POSITION_GRASP_FRAME, [1.0]])
        )[:3]
        - mesh_centroid
    )

    # Draw mesh, ensure -centroid offset so that mesh centroid is centered at origin
    fig.add_trace(
        create_plotly_mesh(
            obj_filepath=mesh_filepath,
            scale=mesh_scale,
            offset=-mesh_centroid,
            color="lightpink",
        )
    )

    # Draw line from left_tip to right_tip
    fig.add_trace(
        go.Scatter3d(
            x=[left_tip[0], right_tip[0]],
            y=[left_tip[1], right_tip[1]],
            z=[left_tip[2], right_tip[2]],
            mode="lines",
            line=dict(color="red", width=10),
            name="Grasp (should align with query points)",
        )
    )

    if save_to_wandb:
        wandb_log_plotly_fig(
            plotly_fig=fig, title=f"Detailed Mesh Plot idx={idx_to_visualize}"
        )

    return fig


# %%
if cfg.visualize_data:
    create_datapoint_plotly_fig(
        dataset=full_dataset,
        datapoint_name="full",
        idx_to_visualize=0,
        save_to_wandb=True,
    )

# %%
if cfg.visualize_data:
    create_detailed_plot_with_mesh(
        full_dataset=full_dataset, idx_to_visualize=0, save_to_wandb=True
    )

# %%

# TODO: END OF NEW

# %% [markdown]
# # Visualize Dataset Distribution


# %%
@localscope.mfc
def create_grasp_success_distribution_fig(
    train_dataset: Subset, input_dataset_full_path: str, save_to_wandb: bool = False
):
    try:
        with h5py.File(input_dataset_full_path, "r") as hdf5_file:
            grasp_successes_np = np.array(
                hdf5_file["/grasp_success"][
                    sorted(train_dataset.indices)
                ]  # Must be ascending
            )

        # Plot histogram in plotly
        fig = go.Figure(
            data=[
                go.Histogram(
                    x=grasp_successes_np,
                    name="Grasp Successes",
                    marker_color="blue",
                ),
            ],
            layout=go.Layout(
                title="Distribution of Grasp Successes",
                xaxis=dict(title="Grasp Success"),
                yaxis=dict(title="Frequency"),
            ),
        )
        if save_to_wandb:
            wandb_log_plotly_fig(
                plotly_fig=fig, title="Distribution of Grasp Successes"
            )
        return fig

    except Exception as e:
        print(f"Error: {e}")
        print("Skipping visualization of grasp success distribution")


if cfg.visualize_data:
    create_grasp_success_distribution_fig(
        train_dataset=train_dataset,
        input_dataset_full_path=input_dataset_full_path,
        save_to_wandb=True,
    )


# %%
@localscope.mfc(
    allowed=[
        "INPUT_EXAMPLE_SHAPE",
        "NERF_COORDINATE_START_IDX",
        "NERF_COORDINATE_END_IDX",
        "NERF_DENSITY_START_IDX",
        "NERF_DENSITY_END_IDX",
        "tqdm",
    ]
)
def create_nerf_grid_input_distribution_figs(
    train_loader: DataLoader, save_to_wandb: bool = False
):
    nerf_coordinate_mins, nerf_coordinate_means, nerf_coordinate_maxs = [], [], []
    nerf_density_mins, nerf_density_means, nerf_density_maxs = [], [], []
    for nerf_grid_inputs, _ in tqdm(
        train_loader, desc="Calculating nerf_grid_inputs dataset statistics"
    ):
        assert nerf_grid_inputs.shape[1:] == INPUT_EXAMPLE_SHAPE
        nerf_coordinates = nerf_grid_inputs[
            :, NERF_COORDINATE_START_IDX:NERF_COORDINATE_END_IDX
        ]
        nerf_densities = nerf_grid_inputs[
            :, NERF_DENSITY_START_IDX:NERF_DENSITY_END_IDX
        ]

        nerf_coordinate_mins.append(nerf_coordinates.min().item())
        nerf_coordinate_means.append(nerf_coordinates.mean().item())
        nerf_coordinate_maxs.append(nerf_coordinates.max().item())

        nerf_density_mins.append(nerf_densities.min().item())
        nerf_density_means.append(nerf_densities.mean().item())
        nerf_density_maxs.append(nerf_densities.max().item())

    nerf_coordinate_mins, nerf_coordinate_means, nerf_coordinate_maxs = (
        np.array(nerf_coordinate_mins),
        np.array(nerf_coordinate_means),
        np.array(nerf_coordinate_maxs),
    )
    nerf_coordinate_min = nerf_coordinate_mins.min()
    nerf_coordinate_mean = nerf_coordinate_means.mean()
    nerf_coordinate_max = nerf_coordinate_maxs.max()
    print(f"nerf_coordinate_min: {nerf_coordinate_min}")
    print(f"nerf_coordinate_mean: {nerf_coordinate_mean}")
    print(f"nerf_coordinate_max: {nerf_coordinate_max}")

    nerf_density_mins, nerf_density_means, nerf_density_maxs = (
        np.array(nerf_density_mins),
        np.array(nerf_density_means),
        np.array(nerf_density_maxs),
    )
    nerf_density_min = nerf_density_mins.min()
    nerf_density_mean = nerf_density_means.mean()
    nerf_density_max = nerf_density_maxs.max()
    print(f"nerf_density_min: {nerf_density_min}")
    print(f"nerf_density_mean: {nerf_density_mean}")
    print(f"nerf_density_max: {nerf_density_max}")

    # Coordinates
    coordinates_fig = go.Figure(
        data=[
            go.Histogram(
                x=nerf_coordinate_mins,
                name="Min",
                marker_color="blue",
            ),
            go.Histogram(
                x=nerf_coordinate_means,
                name="Mean",
                marker_color="orange",
            ),
            go.Histogram(
                x=nerf_coordinate_maxs,
                name="Max",
                marker_color="green",
            ),
        ],
        layout=go.Layout(
            title="Distribution of nerf_coordinates (Aggregated to Fit in RAM)",
            xaxis=dict(title="nerf_coordinates"),
            yaxis=dict(title="Frequency"),
            barmode="overlay",
        ),
    )

    # Density
    density_fig = go.Figure(
        data=[
            go.Histogram(
                x=nerf_density_mins,
                name="Min",
                marker_color="blue",
            ),
            go.Histogram(
                x=nerf_density_means,
                name="Mean",
                marker_color="orange",
            ),
            go.Histogram(
                x=nerf_density_maxs,
                name="Max",
                marker_color="green",
            ),
        ],
        layout=go.Layout(
            title="Distribution of nerf_densities (Aggregated to Fit in RAM)",
            xaxis=dict(title="nerf_densities"),
            yaxis=dict(title="Frequency"),
            barmode="overlay",
        ),
    )

    if save_to_wandb:
        wandb_log_plotly_fig(
            plotly_fig=coordinates_fig, title="Distribution of nerf_coordinates"
        )
        wandb_log_plotly_fig(
            plotly_fig=density_fig, title="Distribution of nerf_densities"
        )

    return coordinates_fig, density_fig


# %%
if cfg.visualize_data:
    create_nerf_grid_input_distribution_figs(
        train_loader=train_loader, save_to_wandb=True
    )


# %% [markdown]
# # Create Neural Network Model


# %%
@localscope.mfc
def mlp(
    num_inputs: int,
    num_outputs: int,
    hidden_layers: List[int],
    activation=nn.ReLU,
    output_activation=nn.Identity,
):
    layers = []
    layer_sizes = [num_inputs] + hidden_layers + [num_outputs]
    for i in range(len(layer_sizes) - 1):
        act = activation if i < len(layer_sizes) - 2 else output_activation
        layers += [nn.Linear(layer_sizes[i], layer_sizes[i + 1]), act()]
    return nn.Sequential(*layers)


class Mean(nn.Module):
    def __init__(self, dim: int):
        super().__init__()
        self.dim = dim

    def forward(self, x: torch.Tensor):
        return torch.mean(x, dim=self.dim)


class Max(nn.Module):
    def __init__(self, dim: int):
        super().__init__()
        self.dim = dim

    def forward(self, x: torch.Tensor):
        return torch.max(x, dim=self.dim)


@localscope.mfc
def conv_encoder(
    input_shape: Tuple[int, ...],
    conv_channels: List[int],
    pool_type: PoolType = PoolType.MAX,
    dropout_prob: float = 0.0,
    conv_output_to_1d: ConvOutputTo1D = ConvOutputTo1D.FLATTEN,
    activation=nn.ReLU,
):
    # Input: Either (n_channels, n_dims) or (n_channels, height, width) or (n_channels, depth, height, width)

    # Validate input
    assert 2 <= len(input_shape) <= 4
    n_input_channels = input_shape[0]
    n_spatial_dims = len(input_shape[1:])

    # Layers for different input sizes
    n_spatial_dims_to_conv_layer_map = {1: nn.Conv1d, 2: nn.Conv2d, 3: nn.Conv3d}
    n_spatial_dims_to_maxpool_layer_map = {
        1: nn.MaxPool1d,
        2: nn.MaxPool2d,
        3: nn.MaxPool3d,
    }
    n_spatial_dims_to_avgpool_layer_map = {
        1: nn.AvgPool1d,
        2: nn.AvgPool2d,
        3: nn.AvgPool3d,
    }
    n_spatial_dims_to_dropout_layer_map = {
        # 1: nn.Dropout1d,  # Not in some versions of torch
        2: nn.Dropout2d,
        3: nn.Dropout3d,
    }
    n_spatial_dims_to_adaptivemaxpool_layer_map = {
        1: nn.AdaptiveMaxPool1d,
        2: nn.AdaptiveMaxPool2d,
        3: nn.AdaptiveMaxPool3d,
    }
    n_spatial_dims_to_adaptiveavgpool_layer_map = {
        1: nn.AdaptiveMaxPool1d,
        2: nn.AdaptiveMaxPool2d,
        3: nn.AdaptiveMaxPool3d,
    }

    # Setup layer types
    conv_layer = n_spatial_dims_to_conv_layer_map[n_spatial_dims]
    if pool_type == PoolType.MAX:
        pool_layer = n_spatial_dims_to_maxpool_layer_map[n_spatial_dims]
    elif pool_type == PoolType.AVG:
        pool_layer = n_spatial_dims_to_avgpool_layer_map[n_spatial_dims]
    else:
        raise ValueError(f"Invalid pool_type = {pool_type}")
    dropout_layer = n_spatial_dims_to_dropout_layer_map[n_spatial_dims]

    # Conv layers
    layers = []
    n_channels = [n_input_channels] + conv_channels
    for i in range(len(n_channels) - 1):
        layers += [
            conv_layer(
                in_channels=n_channels[i],
                out_channels=n_channels[i + 1],
                kernel_size=3,
                stride=1,
                padding="same",
            ),
            activation(),
            pool_layer(kernel_size=2, stride=2),
        ]
        if dropout_prob != 0.0:
            layers += [dropout_layer(p=dropout_prob)]

    # Convert from (n_channels, X) => (Y,)
    if conv_output_to_1d == ConvOutputTo1D.FLATTEN:
        layers.append(nn.Flatten(start_dim=1))
    elif conv_output_to_1d == ConvOutputTo1D.AVG_POOL_SPATIAL:
        adaptiveavgpool_layer = n_spatial_dims_to_adaptiveavgpool_layer_map[
            n_spatial_dims
        ]
        layers.append(
            adaptiveavgpool_layer(output_size=tuple([1 for _ in range(n_spatial_dims)]))
        )
        layers.append(nn.Flatten(start_dim=1))
    elif conv_output_to_1d == ConvOutputTo1D.MAX_POOL_SPATIAL:
        adaptivemaxpool_layer = n_spatial_dims_to_adaptivemaxpool_layer_map[
            n_spatial_dims
        ]
        layers.append(
            adaptivemaxpool_layer(output_size=tuple([1 for _ in range(n_spatial_dims)]))
        )
        layers.append(nn.Flatten(start_dim=1))
    elif conv_output_to_1d == ConvOutputTo1D.AVG_POOL_CHANNEL:
        channel_dim = 1
        layers.append(Mean(dim=channel_dim))
        layers.append(nn.Flatten(start_dim=1))
    elif conv_output_to_1d == ConvOutputTo1D.MAX_POOL_CHANNEL:
        channel_dim = 1
        layers.append(Max(dim=channel_dim))
        layers.append(nn.Flatten(start_dim=1))
    else:
        raise ValueError(f"Invalid conv_output_to_1d = {conv_output_to_1d}")

    return nn.Sequential(*layers)


# %%
class NeRF_to_Grasp_Success_Model(nn.Module):
    def __init__(
        self,
        input_example_shape: Tuple[int, ...],
        neural_network_config: NeuralNetworkConfig,
    ):
        super().__init__()
        self.input_example_shape = input_example_shape
        self.neural_network_config = neural_network_config

        self.conv = conv_encoder(
            input_shape=input_example_shape,
            conv_channels=neural_network_config.conv_channels,
            pool_type=neural_network_config.pool_type,
            dropout_prob=neural_network_config.dropout_prob,
            conv_output_to_1d=neural_network_config.conv_output_to_1d,
        )

        # Get conv output shape
        example_batch_size = 1
        example_input = torch.zeros((example_batch_size, *input_example_shape))
        conv_output = self.conv(example_input)
        assert (
            len(conv_output.shape) == 2 and conv_output.shape[0] == example_batch_size
        )
        _, conv_output_dim = conv_output.shape

        N_CLASSES = 2
        self.mlp = mlp(
            num_inputs=conv_output_dim,
            num_outputs=N_CLASSES,
            hidden_layers=neural_network_config.mlp_hidden_layers,
        )

    @localscope.mfc
    def forward(self, x: torch.Tensor):
        x = self.conv(x)
        x = self.mlp(x)
        return x

    @localscope.mfc
    def get_success_logits(self, x: torch.Tensor):
        return self.forward(x)

    @localscope.mfc
    def get_success_probability(self, x: torch.Tensor):
        return nn.functional.softmax(self.get_success_logits(x), dim=-1)


# %%
device = "cuda" if torch.cuda.is_available() else "cpu"

nerf_to_grasp_success_model = NeRF_to_Grasp_Success_Model(
    input_example_shape=INPUT_EXAMPLE_SHAPE,
    neural_network_config=cfg.neural_network,
).to(device)

optimizer = torch.optim.AdamW(
    params=nerf_to_grasp_success_model.parameters(),
    lr=cfg.training.lr,
)

start_epoch = 0

# %% [markdown]
# # Load Checkpoint

# %%
checkpoint = load_checkpoint(checkpoint_workspace_dir_path)
if checkpoint is not None:
    print("Loading checkpoint...")
    nerf_to_grasp_success_model.load_state_dict(
        checkpoint["nerf_to_grasp_success_model"]
    )
    optimizer.load_state_dict(checkpoint["optimizer"])
    start_epoch = checkpoint["epoch"]
    print("Done loading checkpoint")


# %% [markdown]
# # Visualize Neural Network Model

# %%
print(f"nerf_to_grasp_success_model = {nerf_to_grasp_success_model}")
print(f"optimizer = {optimizer}")

# %%
example_batch_nerf_input, _ = next(iter(train_loader))
example_batch_nerf_input = example_batch_nerf_input.to(device)
print(f"example_batch_nerf_input.shape = {example_batch_nerf_input.shape}")

summary(
    nerf_to_grasp_success_model,
    input_data=example_batch_nerf_input,
    device=device,
    depth=5,
)

# %%
example_batch_nerf_input, _ = next(iter(train_loader))
example_batch_nerf_input = example_batch_nerf_input.requires_grad_(True).to(device)
example_grasp_success_prediction = nerf_to_grasp_success_model(example_batch_nerf_input)

dot = None
try:
    dot = make_dot(
        example_grasp_success_prediction,
        params={
            **dict(nerf_to_grasp_success_model.named_parameters()),
            **{"NERF INPUT": example_batch_nerf_input},
            **{"GRASP SUCCESS": example_grasp_success_prediction},
        },
    )
    model_graph_filename = "model_graph.png"
    model_graph_filename_split = model_graph_filename.split(".")
    print(f"Saving to {model_graph_filename}...")
    dot.render(model_graph_filename_split[0], format=model_graph_filename_split[1])
    print(f"Done saving to {model_graph_filename}")
except:
    print("Failed to save model graph to file.")

dot


# %% [markdown]
# # Training Setup


# %%
@localscope.mfc
def save_checkpoint(
    checkpoint_workspace_dir_path: str,
    epoch: int,
    nerf_to_grasp_success_model: NeRF_to_Grasp_Success_Model,
    optimizer: torch.optim.Optimizer,
):
    checkpoint_filepath = os.path.join(
        checkpoint_workspace_dir_path, f"checkpoint_{epoch:04}.pt"
    )
    print(f"Saving checkpoint to {checkpoint_filepath}")
    torch.save(
        {
            "epoch": epoch,
            "nerf_to_grasp_success_model": nerf_to_grasp_success_model.state_dict(),
            "optimizer": optimizer.state_dict(),
        },
        checkpoint_filepath,
    )
    print("Done saving checkpoint")


# %%


@localscope.mfc
def create_dataloader_subset(
    original_dataloader: DataLoader,
    fraction: Optional[float] = None,
    subset_size: Optional[int] = None,
) -> DataLoader:
    if fraction is not None and subset_size is None:
        smaller_dataset_size = int(len(original_dataloader.dataset) * fraction)
    elif fraction is None and subset_size is not None:
        smaller_dataset_size = subset_size
    else:
        raise ValueError(f"Must specify either fraction or subset_size")

    sampled_indices = random.sample(
        range(len(original_dataloader.dataset.indices)), smaller_dataset_size
    )
    dataloader = DataLoader(
        original_dataloader.dataset,
        batch_size=original_dataloader.batch_size,
        sampler=SubsetRandomSampler(
            sampled_indices,
        ),
        pin_memory=original_dataloader.pin_memory,
        num_workers=original_dataloader.num_workers,
    )
    return dataloader


@localscope.mfc(allowed=["tqdm"])
def iterate_through_dataloader(
    phase: Phase,
    dataloader: DataLoader,
    nerf_to_grasp_success_model: NeRF_to_Grasp_Success_Model,
    device: str,
    ce_loss_fn: nn.CrossEntropyLoss,
    wandb_log_dict: Dict[str, Any],
    cfg: Optional[TrainingConfig] = None,
    optimizer: Optional[torch.optim.Optimizer] = None,
    log_loss: bool = True,
    log_grad: bool = False,
    gather_predictions: bool = False,
    log_confusion_matrix: bool = False,
):
    assert phase in [Phase.TRAIN, Phase.VAL, Phase.TEST]
    if phase == Phase.TRAIN:
        nerf_to_grasp_success_model.train()
        assert cfg is not None and optimizer is not None

    else:
        nerf_to_grasp_success_model.eval()
        assert cfg is None and optimizer is None

    with torch.set_grad_enabled(phase == Phase.TRAIN):
        losses_dict = defaultdict(list)
        grads_dict = defaultdict(list)

        batch_total_time_taken = 0.0
        dataload_total_time_taken = 0.0
        forward_pass_total_time_taken = 0.0
        backward_pass_total_time_taken = 0.0
        grad_log_total_time_taken = 0.0
        loss_log_total_time_taken = 0.0
        gather_predictions_total_time_taken = 0.0

        all_predictions, all_ground_truths = [], []

        end_time = time.time()
        for nerf_grid_inputs, grasp_successes in (pbar := tqdm(dataloader)):
            dataload_time_taken = time.time() - end_time

            # Forward pass
            start_forward_pass_time = time.time()
            nerf_grid_inputs = nerf_grid_inputs.to(device)
            grasp_successes = grasp_successes.to(device)

            grasp_success_logits = nerf_to_grasp_success_model.get_success_logits(
                nerf_grid_inputs
            )
            ce_loss = ce_loss_fn(input=grasp_success_logits, target=grasp_successes)
            total_loss = ce_loss
            forward_pass_time_taken = time.time() - start_forward_pass_time

            # Gradient step
            start_backward_pass_time = time.time()
            if phase == Phase.TRAIN and optimizer is not None:
                optimizer.zero_grad()
                total_loss.backward()

                if cfg is not None and cfg.grad_clip_val is not None:
                    torch.nn.utils.clip_grad_value_(
                        nerf_to_grasp_success_model.parameters(),
                        cfg.grad_clip_val,
                    )

                optimizer.step()
            backward_pass_time_taken = time.time() - start_backward_pass_time

            # Loss logging
            start_loss_log_time = time.time()
            if log_loss:
                losses_dict[f"{phase.name.lower()}_loss"].append(total_loss.item())
            loss_log_time_taken = time.time() - start_loss_log_time

            # Gradient logging
            start_grad_log_time = time.time()
            if phase == Phase.TRAIN and log_grad:
                with torch.no_grad():  # not sure if need this
                    grad_abs_values = torch.concat(
                        [
                            p.grad.data.abs().flatten()
                            for p in nerf_to_grasp_success_model.parameters()
                            if p.grad is not None and p.requires_grad
                        ]
                    )
                    grads_dict[f"{phase.name.lower()}_max_grad_abs_value"].append(
                        torch.max(grad_abs_values).item()
                    )
                    grads_dict[f"{phase.name.lower()}_median_grad_abs_value"].append(
                        torch.median(grad_abs_values).item()
                    )
                    grads_dict[f"{phase.name.lower()}_mean_grad_abs_value"].append(
                        torch.mean(grad_abs_values).item()
                    )
                    grads_dict[f"{phase.name.lower()}_mean_grad_norm_value"].append(
                        torch.norm(grad_abs_values).item()
                    )
            grad_log_time_taken = time.time() - start_grad_log_time

            # Gather predictions and ground truths
            start_gather_predictions_time = time.time()
            if gather_predictions:
                with torch.no_grad():
                    predictions = grasp_success_logits.argmax(axis=1).tolist()
                    ground_truths = grasp_successes.tolist()
                    all_predictions = all_predictions + predictions
                    all_ground_truths = all_ground_truths + ground_truths
            gather_predictions_time_taken = time.time() - start_gather_predictions_time

            batch_time_taken = time.time() - end_time

            # Set description
            loss_log_str = (
                f"loss: {np.mean(losses_dict[f'{phase.name.lower()}_loss']):.5f}"
                if len(losses_dict[f"{phase.name.lower()}_loss"]) > 0
                else "loss: N/A"
            )
            description = " | ".join(
                [
                    f"{phase.name.lower()} (ms)",
                    f"Batch: {1000*batch_time_taken:.0f}",
                    f"Data: {1000*dataload_time_taken:.0f}",
                    f"Fwd: {1000*forward_pass_time_taken:.0f}",
                    f"Bwd: {1000*backward_pass_time_taken:.0f}",
                    f"Loss: {1000*loss_log_time_taken:.0f}",
                    f"Grad: {1000*grad_log_time_taken:.0f}",
                    f"Gather: {1000*gather_predictions_time_taken:.0f}",
                    loss_log_str,
                ]
            )
            pbar.set_description(description)

            batch_total_time_taken += batch_time_taken
            dataload_total_time_taken += dataload_time_taken
            forward_pass_total_time_taken += forward_pass_time_taken
            backward_pass_total_time_taken += backward_pass_time_taken
            loss_log_total_time_taken += loss_log_time_taken
            grad_log_total_time_taken += grad_log_time_taken
            gather_predictions_total_time_taken += gather_predictions_time_taken

            end_time = time.time()

    print(
        f"Total time taken for {phase.name.lower()} phase: {batch_total_time_taken:.2f} s"
    )
    print(f"Time taken for dataload: {dataload_total_time_taken:.2f} s")
    print(f"Time taken for forward pass: {forward_pass_total_time_taken:.2f} s")
    print(f"Time taken for backward pass: {backward_pass_total_time_taken:.2f} s")
    print(f"Time taken for loss logging: {loss_log_total_time_taken:.2f} s")
    print(f"Time taken for grad logging: {grad_log_total_time_taken:.2f} s")
    print(
        f"Time taken for gather predictions: {gather_predictions_total_time_taken:.2f} s"
    )
    print()

    # In percentage of batch_total_time_taken
    print("In percentage of batch_total_time_taken:")
    print(f"dataload: {100*dataload_total_time_taken/batch_total_time_taken:.2f} %")
    print(
        f"forward pass: {100*forward_pass_total_time_taken/batch_total_time_taken:.2f} %"
    )
    print(
        f"backward pass: {100*backward_pass_total_time_taken/batch_total_time_taken:.2f} %"
    )
    print(f"loss logging: {100*loss_log_total_time_taken/batch_total_time_taken:.2f} %")
    print(f"grad logging: {100*grad_log_total_time_taken/batch_total_time_taken:.2f} %")
    print(
        f"gather predictions: {100*gather_predictions_total_time_taken/batch_total_time_taken:.2f} %"
    )
    print()
    print()

    if log_confusion_matrix and len(all_predictions) > 0 and len(all_ground_truths) > 0:
        wandb_log_dict[
            f"{phase.name.lower()}_confusion_matrix"
        ] = wandb.plot.confusion_matrix(
            preds=all_predictions,
            y_true=all_ground_truths,
            class_names=["Fail", "Success"],
            title=f"{phase.name.lower()} Confusion Matrix",
        )

    for loss_name, losses in losses_dict.items():
        wandb_log_dict[loss_name] = np.mean(losses)

    if len(all_predictions) > 0 and len(all_ground_truths) > 0:
        # Can add more metrics here
        wandb_log_dict[f"{phase.name.lower()}_accuracy"] = 100.0 * accuracy_score(
            y_true=all_ground_truths, y_pred=all_predictions
        )

    # Extra debugging
    for grad_name, grad_vals in grads_dict.items():
        if "_max_" in grad_name:
            wandb_log_dict[grad_name] = np.max(grad_vals)
        elif "_mean_" in grad_name:
            wandb_log_dict[grad_name] = np.mean(grad_vals)
        elif "_median_" in grad_name:
            wandb_log_dict[grad_name] = np.median(grad_vals)
        else:
            print(f"WARNING: grad_name = {grad_name} will not be logged")

    return


# %%
@torch.no_grad()
@localscope.mfc(allowed=["tqdm"])
def plot_confusion_matrix(
    phase: Phase,
    dataloader: DataLoader,
    nerf_to_grasp_success_model: NeRF_to_Grasp_Success_Model,
    device: str,
    wandb_log_dict: Dict[str, Any],
):
    # TODO: This is very slow and wasteful if we already compute all these in the other iterate_through_dataloader function calls
    preds, ground_truths = [], []
    for nerf_grid_inputs, grasp_successes in (pbar := tqdm(dataloader)):
        nerf_grid_inputs = nerf_grid_inputs.to(device)
        pbar.set_description(f"{phase.name.lower()} Confusion Matrix")
        pred = (
            nerf_to_grasp_success_model.get_success_probability(nerf_grid_inputs)
            .argmax(axis=1)
            .tolist()
        )
        ground_truth = grasp_successes.tolist()
        preds, ground_truths = preds + pred, ground_truths + ground_truth

    wandb_log_dict[
        f"{phase.name.lower()}_confusion_matrix"
    ] = wandb.plot.confusion_matrix(
        preds=preds,
        y_true=ground_truths,
        class_names=["Fail", "Success"],
        title=f"{phase.name.lower()} Confusion Matrix",
    )

    preds = torch.tensor(preds)
    ground_truths = torch.tensor(ground_truths)
    num_correct = torch.sum(preds == ground_truths).item()
    num_datapoints = len(preds)
    wandb_log_dict[f"{phase.name.lower()}_accuracy"] = (
        num_correct / num_datapoints * 100
    )


# %% [markdown]
# # Training


# %%
@localscope.mfc(allowed=["tqdm"])
def run_training_loop(
    cfg: TrainingConfig,
    train_loader: DataLoader,
    val_loader: DataLoader,
    nerf_to_grasp_success_model: NeRF_to_Grasp_Success_Model,
    device: str,
    ce_loss_fn: nn.CrossEntropyLoss,
    optimizer: torch.optim.Optimizer,
    start_epoch: int,
    checkpoint_workspace_dir_path: str,
):
    training_loop_base_description = "Training Loop"
    for epoch in (
        pbar := tqdm(
            range(start_epoch, cfg.n_epochs), desc=training_loop_base_description
        )
    ):
        wandb_log_dict = {}
        wandb_log_dict["epoch"] = epoch

        # Save checkpoint
        start_save_checkpoint_time = time.time()
        if epoch % cfg.save_checkpoint_freq == 0 and (
            epoch != 0 or cfg.save_checkpoint_on_epoch_0
        ):
            save_checkpoint(
                checkpoint_workspace_dir_path=checkpoint_workspace_dir_path,
                epoch=epoch,
                nerf_to_grasp_success_model=nerf_to_grasp_success_model,
                optimizer=optimizer,
            )
        save_checkpoint_time_taken = time.time() - start_save_checkpoint_time

        log_confusion_matrix = epoch % cfg.confusion_matrix_freq == 0 and (
            epoch != 0 or cfg.save_confusion_matrix_on_epoch_0
        )
        gather_predictions = log_confusion_matrix

        # Train
        start_train_time = time.time()
        log_grad = epoch % cfg.log_grad_freq == 0 and (
            epoch != 0 or cfg.log_grad_on_epoch_0
        )
        if cfg.use_dataloader_subset:
            # subset_fraction = 0.2
            # subset_train_loader = create_dataloader_subset(
            #     train_loader, fraction=subset_fraction
            # )
            # num_passes = int(1 / subset_fraction)
            subset_train_loader = create_dataloader_subset(
                train_loader,
                subset_size=32_000,  # 2023-04-28 each datapoint is 1MB
            )
            num_passes = 3
            for subset_pass in range(num_passes):
                print(f"Subset pass {subset_pass + 1}/{num_passes}")
                iterate_through_dataloader(
                    phase=Phase.TRAIN,
                    dataloader=subset_train_loader,
                    nerf_to_grasp_success_model=nerf_to_grasp_success_model,
                    device=device,
                    ce_loss_fn=ce_loss_fn,
                    wandb_log_dict=wandb_log_dict,
                    cfg=cfg,
                    optimizer=optimizer,
                    log_grad=log_grad,
                    gather_predictions=False,  # Doesn't make sense to gather predictions for a subset
                    log_confusion_matrix=False,
                )
        else:
            iterate_through_dataloader(
                phase=Phase.TRAIN,
                dataloader=train_loader,
                nerf_to_grasp_success_model=nerf_to_grasp_success_model,
                device=device,
                ce_loss_fn=ce_loss_fn,
                wandb_log_dict=wandb_log_dict,
                cfg=cfg,
                optimizer=optimizer,
                log_grad=log_grad,
                gather_predictions=gather_predictions,
                log_confusion_matrix=log_confusion_matrix,
            )
        train_time_taken = time.time() - start_train_time

        # Val
        # Can do this before or after training (decided on after since before it was always at -ln(1/N_CLASSES) ~ 0.69)
        start_val_time = time.time()
        if epoch % cfg.val_freq == 0 and (epoch != 0 or cfg.val_on_epoch_0):
            iterate_through_dataloader(
                phase=Phase.VAL,
                dataloader=val_loader,
                nerf_to_grasp_success_model=nerf_to_grasp_success_model,
                device=device,
                ce_loss_fn=ce_loss_fn,
                wandb_log_dict=wandb_log_dict,
                gather_predictions=gather_predictions,
                log_confusion_matrix=log_confusion_matrix,
            )
        val_time_taken = time.time() - start_val_time

        wandb.log(wandb_log_dict)

        # Set description
        description = " | ".join(
            [
                training_loop_base_description + " (s)",
                f"Save: {save_checkpoint_time_taken:.0f}",
                f"Train: {train_time_taken:.0f}",
                f"Val: {val_time_taken:.0f}",
            ]
        )
        pbar.set_description(description)


# %%
wandb.watch(nerf_to_grasp_success_model, log="gradients", log_freq=100)


# %%
@localscope.mfc
def compute_class_weight_np(train_dataset: Subset, input_dataset_full_path: str):
    try:
        print("Loading grasp success data for class weighting...")
        t1 = time.time()
        with h5py.File(input_dataset_full_path, "r") as hdf5_file:
            grasp_successes_np = np.array(hdf5_file["/grasp_success"][()])
        t2 = time.time()
        print(f"Loaded grasp success data in {t2 - t1:.2f} s")

        print("Extracting training indices...")
        t3 = time.time()
        grasp_successes_np = grasp_successes_np[train_dataset.indices]
        t4 = time.time()
        print(f"Extracted training indices in {t4 - t3:.2f} s")

        print("Computing class weight with this data...")
        t5 = time.time()
        class_weight_np = compute_class_weight(
            class_weight="balanced",
            classes=np.unique(grasp_successes_np),
            y=grasp_successes_np,
        )
        t6 = time.time()
        print(f"Computed class weight in {t6 - t5:.2f} s")

    except Exception as e:
        print(f"Failed to compute class weight: {e}")
        print("Using default class weight")
        class_weight_np = np.array([1.0, 1.0])
    return class_weight_np


class_weight = (
    torch.from_numpy(
        compute_class_weight_np(
            train_dataset=train_dataset, input_dataset_full_path=input_dataset_full_path
        )
    )
    .float()
    .to(device)
)
print(f"Class weight: {class_weight}")
ce_loss_fn = nn.CrossEntropyLoss(weight=class_weight)

# %%
run_training_loop(
    cfg=cfg.training,
    train_loader=train_loader,
    val_loader=val_loader,
    nerf_to_grasp_success_model=nerf_to_grasp_success_model,
    device=device,
    ce_loss_fn=ce_loss_fn,
    optimizer=optimizer,
    start_epoch=start_epoch,
    checkpoint_workspace_dir_path=checkpoint_workspace_dir_path,
)

# %% [markdown]
# # Test

# %%
nerf_to_grasp_success_model.eval()
wandb_log_dict = {}
print(f"Running test metrics on epoch {cfg.training.n_epochs}")
wandb_log_dict["epoch"] = cfg.training.n_epochs
iterate_through_dataloader(
    phase=Phase.TEST,
    dataloader=test_loader,
    nerf_to_grasp_success_model=nerf_to_grasp_success_model,
    device=device,
    ce_loss_fn=ce_loss_fn,
    wandb_log_dict=wandb_log_dict,
    gather_predictions=True,
    log_confusion_matrix=True,
)

wandb.log(wandb_log_dict)

# %% [markdown]
# # Save Model

# %%
save_checkpoint(
    checkpoint_workspace_dir_path=checkpoint_workspace_dir_path,
    epoch=cfg.training.n_epochs,
    nerf_to_grasp_success_model=nerf_to_grasp_success_model,
    optimizer=optimizer,
)

# %%
