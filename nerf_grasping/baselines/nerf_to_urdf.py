import tyro
import pathlib
import subprocess
import numpy as np
from typing import Optional

from dataclasses import dataclass
from nerf_grasping.baselines.nerf_to_mesh import nerf_to_mesh
from nerf_grasping.grasp_utils import load_nerf_field
from typing import Tuple


@dataclass
class Args:
    nerfcheckpoint_filepath: pathlib.Path
    bounding_cube_half_length: float = 0.2
    density_of_0_level_set: float = 15.0
    n_pts_each_dim_marching_cubes: int = 31
    rescale: bool = True
    min_num_edges: Optional[int] = 100
    output_dir_path: pathlib.Path = pathlib.Path(__file__).parent / "nerf_meshdata"


def print_and_run(cmd: str) -> None:
    print(cmd)
    subprocess.run(cmd, shell=True)


def create_urdf(
    obj_path: pathlib.Path,
    output_urdf_filename: str,
    ixx: float = 0.1,
    iyy: float = 0.1,
    izz: float = 0.1,
) -> pathlib.Path:
    assert obj_path.exists(), f"{obj_path} does not exist"
    assert output_urdf_filename.endswith(
        ".urdf"
    ), f"{output_urdf_filename} does not end with .urdf"
    output_folder = obj_path.parent
    obj_filename = obj_path.name

    urdf_content = f"""<robot name="root">
  <link name="base_link">
    <inertial>
      <origin xyz="0 0 0" rpy="0 0 0"/>
      <inertia ixx="{ixx}" ixy="0" ixz="0" iyy="{iyy}" iyz="0" izz="{izz}"/>
    </inertial>
    <visual>
      <origin xyz="0 0 0" rpy="0 0 0"/>
      <geometry>
        <mesh filename="{obj_filename}" scale="1 1 1"/>
      </geometry>
      <material name="">
        <color rgba="0.75 0.75 0.75 1"/>
      </material>
    </visual>
    <collision>
      <origin xyz="0 0 0" rpy="0 0 0"/>
      <geometry>
        <mesh filename="{obj_filename}" scale="1 1 1"/>
      </geometry>
    </collision>
  </link>
</robot>"""

    output_urdf_path = output_folder / output_urdf_filename
    assert not output_urdf_path.exists(), f"{output_urdf_path} already exists"
    with open(output_urdf_path, "w") as urdf_file:
        urdf_file.write(urdf_content)
    return output_urdf_path


def parse_object_code_and_scale(object_code_and_scale_str: str) -> Tuple[str, float]:
    keyword = "_0_"
    idx = object_code_and_scale_str.rfind(keyword)
    object_code = object_code_and_scale_str[:idx]

    idx_offset_for_scale = keyword.index("0")
    object_scale = float(
        object_code_and_scale_str[idx + idx_offset_for_scale :].replace("_", ".")
    )
    return object_code, object_scale


def main() -> None:
    args = tyro.cli(Args)
    print("=" * 80)
    print(f"{pathlib.Path(__file__).name} args: {args}")
    print("=" * 80 + "\n")

    assert (
        args.nerfcheckpoint_filepath.exists()
    ), f"{args.nerfcheckpoint_filepath} does not exist"
    assert (
        args.nerfcheckpoint_filepath.name == "config.yml"
    ), f"{args.nerfcheckpoint_filepath} is not a config.yml file"
    assert (
        args.nerfcheckpoint_filepath.parent.parent.name == "nerfacto"
    ), f"{args.nerfcheckpoint_filepath.parent.parent.name} should be nerfacto"
    # Eg. path=data/2023-10-13_13-12-28/nerfcheckpoints/sem-RubiksCube-1e3d89eb3b5427053bdd31f1cd9eec98_0_1076/nerfacto/2023-10-13_131849/config.yml
    # object_code_and_scale=sem-RubiksCube-1e3d89eb3b5427053bdd31f1cd9eec98_0_1076
    object_code_and_scale = args.nerfcheckpoint_filepath.parent.parent.parent.name
    object_code, object_scale = parse_object_code_and_scale(object_code_and_scale)

    nerf_field = load_nerf_field(args.nerfcheckpoint_filepath)
    lb = -args.bounding_cube_half_length * np.ones(3)
    ub = args.bounding_cube_half_length * np.ones(3)

    # Should match existing meshdata folder structure
    # <output_dir_path>
    # └── <object_code>
    #     └── coacd
    #         ├── coacd.urdf
    #         └── decomposed.obj
    output_folder = args.output_dir_path / object_code / "coacd"
    output_folder.mkdir(exist_ok=False, parents=True)

    obj_path = output_folder / "decomposed.obj"
    scale = 1.0 / object_scale if args.rescale else 1.0
    nerf_to_mesh(
        nerf_field,
        level=args.density_of_0_level_set,
        npts=args.n_pts_each_dim_marching_cubes,
        lb=lb,
        ub=ub,
        scale=scale,
        min_len=args.min_num_edges,
        save_path=obj_path,
    )

    urdf_path = create_urdf(obj_path=obj_path, output_urdf_filename="coacd.urdf")

    assert urdf_path.exists(), f"{urdf_path} does not exist"
    assert obj_path.exists(), f"{obj_path} does not exist"
    print(f"Created {urdf_path} and {obj_path}")


if __name__ == "__main__":
    main()
