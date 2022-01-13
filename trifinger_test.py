
import os
import json
import math
import numpy as np
# import mathutils
from PIL import Image
from pathlib import Path
import shutil

from isaacgym import gymapi, gymutil

import matplotlib.pyplot as plt

# https://github.com/NVIDIA-Omniverse/IsaacGymEnvs

class TriFingerEnv:

    def __init__(self, viewer = True):
        self.args = gymutil.parse_arguments( description="Trifinger test",)
        self.gym = gymapi.acquire_gym()

        self.setup_sim()
        self.setup_envs()

        if viewer:
            self.setup_viewer()
        else:
            self.viewer = None

        self.gym.prepare_sim(self.sim)

    def setup_sim(self):
        #only tested with this one
        assert self.args.physics_engine == gymapi.SIM_PHYSX

        # configure sim
        sim_params = gymapi.SimParams()
        sim_params.dt = dt = 1.0 / 60.0

        sim_params.up_axis = gymapi.UP_AXIS_Z
        sim_params.gravity = gymapi.Vec3(0.0, 0.0, -9.8)

        sim_params.physx.solver_type = 1
        sim_params.physx.num_position_iterations = 6
        sim_params.physx.num_velocity_iterations = 0
        sim_params.physx.num_threads = self.args.num_threads
        sim_params.physx.use_gpu = self.args.use_gpu

        # allows for non-convex objects but has other issues
        # self.args.physics_engine = gymapi.SIM_FLEX
        # sim_params.flex.solver_type = 5
        # sim_params.flex.num_outer_iterations = 4
        # sim_params.flex.num_inner_iterations = 20
        # sim_params.flex.relaxation = 0.8
        # sim_params.flex.warm_start = 0.5

        sim_params.use_gpu_pipeline = True
        self.sim = self.gym.create_sim(self.args.compute_device_id,
                                       self.args.graphics_device_id,
                                       self.args.physics_engine,
                                       sim_params)
        assert self.sim != None

    def setup_envs(self):
        plane_params = gymapi.PlaneParams()
        plane_params.normal = gymapi.Vec3(0, 0, 1) # z-up!
        self.gym.add_ground(self.sim, plane_params)

        spacing = 1.0
        env_lower = gymapi.Vec3(-spacing, -spacing, 0.0)
        env_upper = gymapi.Vec3(spacing, spacing, spacing)
        env = self.gym.create_env(self.sim, env_lower, env_upper, 0)

        self.env = env # used only when there is one env
        self.envs = [env]

        self.setup_robot(env)
        self.setup_stage(env)
        self.setup_object(env)
        self.setup_cameras(env)


    def setup_stage(self, env):
        asset_dir = 'assets'

        stage_urdf_file = "trifinger/robot_properties_fingers/urdf/high_table_boundary.urdf" #this one is convex decomposed
        # stage_urdf_file = "trifinger/robot_properties_fingers/urdf/trifinger_stage.urdf"
        # stage_urdf_file = "trifinger/robot_properties_fingers/urdf/stage.urdf"

        asset_options = gymapi.AssetOptions()
        asset_options.disable_gravity = True
        asset_options.fix_base_link = True
        asset_options.flip_visual_attachments = False
        asset_options.use_mesh_materials = True
        asset_options.thickness = 0.001

        stage_asset = self.gym.load_asset(self.sim, asset_dir, stage_urdf_file, asset_options)
        self.gym.create_actor(env, stage_asset, gymapi.Transform(), "Stage", 0, 0)

    def setup_robot(self, env):
        asset_dir = 'assets'
        robot_urdf_file = "trifinger/robot_properties_fingers/urdf/pro/trifingerpro.urdf"
        # robot_urdf_file = "trifinger/robot_properties_fingers/urdf/trifinger_with_stage.urdf"

        asset_options = gymapi.AssetOptions()
        asset_options.fix_base_link = True
        asset_options.flip_visual_attachments = False
        asset_options.use_mesh_materials = True
        asset_options.thickness = 0.001

        robot_asset = self.gym.load_asset(self.sim, asset_dir, robot_urdf_file, asset_options)
        self.gym.create_actor(env, robot_asset, gymapi.Transform(), "Trifinger", 0, 0)

    def setup_object(self, env):
        asset_dir = 'assets'
        teady_bear_file = "objects/urdf/teady_bear.urdf"

        asset_options = gymapi.AssetOptions()
        # asset_options.thickness = 0.001

        asset_options.vhacd_enabled = True
        asset_options.mesh_normal_mode = gymapi.COMPUTE_PER_VERTEX
        asset_options.override_inertia = True
        asset_options.override_com = True

        asset_options.vhacd_params.mode = 0
        asset_options.vhacd_params.resolution = 300000
        asset_options.vhacd_params.max_convex_hulls = 10
        asset_options.vhacd_params.max_num_vertices_per_ch = 16

        sphere_asset = self.gym.create_sphere(self.sim, 0.1, asset_options)
        teady_bear_asset = self.gym.load_asset(self.sim, asset_dir, teady_bear_file, asset_options)

        # gym.create_actor(env, sphere_asset, gymapi.Transform(p=gymapi.Vec3(0., 0., 1.)), "sphere", 0, 0)
        self.gym.create_actor(env, teady_bear_asset, gymapi.Transform(p=gymapi.Vec3(0., 0., 0.1)), "teady bear", 0, 0)

    def setup_viewer(self):
        self.viewer = self.gym.create_viewer(self.sim, gymapi.CameraProperties())
        assert self.viewer != None

        cam_pos = gymapi.Vec3(0.5, 0.5, 0.7)
        # cam_pos = gymapi.Vec3(1, 1, 1.5)
        cam_target = gymapi.Vec3(0, 0, 0.2)
        # self.gym.viewer_camera_look_at(self.viewer, self.env, cam_pos, cam_target)
        self.gym.viewer_camera_look_at(self.viewer, self.env, gymapi.Vec3(1,1,1.5), gymapi.Vec3(0,0,0))

    def setup_cameras(self, env):
        camera_props = gymapi.CameraProperties()
        camera_props.horizontal_fov = 35.0
        camera_props.width = 400
        camera_props.height = 400
        
        # generates cameara positions along rings around object
        heights   = [0.3, 0.9, 1.]
        distances = [0.4, 0.5, 0.1]
        counts    = [5,   6,    1]

        camera_positions = []
        for h,d,c in zip(heights, distances, counts):
            for alpha in np.linspace(0, 2*np.pi, c, endpoint=False):
                camera_positions.append( [d* np.sin(alpha), d*np.cos(alpha), h] )

        self.camera_handles = []
        for pos in camera_positions:
            camera_handle = self.gym.create_camera_sensor(env, camera_props)
            self.gym.set_camera_location(camera_handle, env, gymapi.Vec3(*pos), gymapi.Vec3(0,0,0.1))

            self.camera_handles.append(camera_handle)

    def save_images(self, folder):
        self.gym.render_all_camera_sensors(self.sim)

        path = Path(folder)

        if path.exists():
            print(path, "already exists!")
            if input("Clear it before continuing? [y/N]:").lower() == "y":
                shutil.rmtree(path)

        path.mkdir()

        for i,camera_handle in enumerate(self.camera_handles):
            color_image = self.gym.get_camera_image(self.sim, self.env, camera_handle, gymapi.IMAGE_COLOR)
            color_image = color_image.reshape(400,400,4)

            Image.fromarray(color_image).save(path / f"{i}.png")

            transform = self.gym.get_camera_transform(self.sim, self.env, camera_handle)

            # identity = np.array([gymapi.Vec3(1,0,0),
            #                          gymapi.Vec3(0,1,0),
            #                          gymapi.Vec3(0,0,1),
            #                          gymapi.Vec3(0,0,0),])[None,:]

            # print(type(identity))

            # output = transform.transform_points( identity )
            # matrix = mathutils.Matrix.LocRotScale(transform.p , mathutils.Quaternion(transform.q) , None)

            with open(path / f"{i}.txt", "w+") as f:
                # f.write( str(matrix) )
                # json.dump([ [v.x, v.y, v.z] for v in output ], f)

                data = [transform.p.x, transform.p.y, transform.p.z, transform.r.x, transform.r.y, transform.r.z, transform.r.w]
                json.dump(data, f)

            # plt.imshow(color_image.reshape(400,400,4))
            # plt.show()

    def get_images(self):
        pass


    def get_object_pose(self):
        pass

    def get_robot_state(self):
        pass

    def do_robot_action(self, action):
        pass


    def step_gym(self):
        self.gym.simulate(self.sim)
        self.gym.fetch_results(self.sim, True)

        self.gym.step_graphics(self.sim)

        if self.viewer != None:
            self.gym.draw_viewer(self.viewer, self.sim, True)
            self.gym.sync_frame_time(self.sim)



if __name__ == "__main__":
    tf = TriFingerEnv()

    while not tf.gym.query_viewer_has_closed(tf.viewer):

        # prototype of inerface
        # tf.get_robot_state()
        # tf.do_robot_action(None)

        tf.step_gym()

    print("closed!")

    # for _ in range(10000):
    #     tf.step_gym()

    tf.save_images("/home/mikadam/Desktop/test")







