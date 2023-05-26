# Set megapose environment variables
import os
import json
print('Before server import')
import megapose_server
megapose_server_install_dir = os.path.dirname(megapose_server.__file__)
variables_file = os.path.join(megapose_server_install_dir, 'megapose_variables.json')
print(megapose_server, variables_file)
with open(variables_file, 'r') as f:
    json_vars = json.load(f)
    print(json_vars)
    os.environ['MEGAPOSE_DIR'] = json_vars['megapose_dir']
    os.environ['MEGAPOSE_DATA_DIR'] = json_vars['megapose_data_dir']


# Third Party
import numpy as np
import argparse
import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

import numpy as np
from PIL import Image

import socket
import struct
import io
import sys
import traceback
from operator import itemgetter
import pandas as pd
import torch
import torch.fx as fx
import torch.nn as nn
from torch.fx.experimental.optimization import optimize_for_inference, fuse

# MegaPose
from megapose.datasets.object_dataset import RigidObject, RigidObjectDataset
from megapose.datasets.scene_dataset import CameraData, ObjectData
from megapose.inference.types import (
    DetectionsType,
    ObservationTensor,
    PoseEstimatesType,
)
from megapose.inference.utils import make_detections_from_object_data
from megapose.lib3d.transform import Transform
from megapose.panda3d_renderer import Panda3dLightData
from megapose.panda3d_renderer.panda3d_scene_renderer import Panda3dSceneRenderer
from megapose.utils.conversion import convert_scene_observation_to_panda3d
from megapose.utils.load_model import NAMED_MODELS, load_named_model

# Megapose server
from megapose_server.network_utils import *
from megapose_server.server_operations import ServerMessage

def make_object_dataset(meshes_dir: Path) -> RigidObjectDataset:
    rigid_objects = []
    mesh_units = "m"
    object_dirs = meshes_dir.iterdir()
    for object_dir in object_dirs:
        label = object_dir.name
        mesh_path = None
        for fn in object_dir.glob("*"):
            if fn.suffix in {".obj", ".ply", ".glb", ".gltf"}:
                assert not mesh_path, f"there are multiple meshes in the {label} directory"
                mesh_path = fn
        assert mesh_path, f"couldnt find a obj or ply mesh for {label}"
        rigid_objects.append(RigidObject(label=label, mesh_path=mesh_path, mesh_units=mesh_units))
    rigid_object_dataset = RigidObjectDataset(rigid_objects)
    return rigid_object_dataset

def fuse_resnet(model: nn.Module):
    '''
    Fuse layers of a resnet in place
    '''
    torch.quantization.fuse_modules(model, [["conv1", "bn1", "relu"]], inplace=True)
    for module_name, module in model.named_children():
        if "layer" in module_name:
            for basic_block_name, basic_block in module.named_children():
                torch.quantization.fuse_modules(basic_block, [["conv1", "bn1", "relu"], ["conv2", "bn2"]], inplace=True)
                for sub_block_name, sub_block in basic_block.named_children():
                    if sub_block_name == "downsample":
                        torch.quantization.fuse_modules(sub_block, [["0", "1"]], inplace=True)

class MegaposeServer():
    '''
    A TCP-based server that can be interrogated to estimate the pose of an object  with MegaPose
    '''
    def __init__(self, host: str, port: int, model_name: str, mesh_dir: Path, camera_data: Dict, optimize: bool):
        """Create a TCP server that listens for an incoming connection
        and can be used to answer client queries about an object pose with respect to the camera frame

        Args:
            host (str): The IP address to which this server will be bound. To connect, the client must be able to see this specific IP address.
            You may listen on all network interfaces if host="0.0.0.0". This however, is dangerous as anyone from anywhere can connect to the server.
            port (int): The port on which the server will listen for an incoming connection
            model_name (str): The name of the megapose model to use. One of:
                ['megapose-1.0-RGB',
                 'megapose-1.0-RGBD',
                 'megapose-1.0-RGB-multi-hypothesis',
                 'megapose-1.0-RGB-multi-hypothesis-icp']
            mesh_dir (Path): The path to the directory containing the 3D models
                Each model is stored in a subfolder, where the subfolder name gives the name of the object
                A 3D model can be in .obj or .ply format. Units are assumed to be in meters.
            camera_data (Dict): The starting camera calibration. A dict containing
                h: the height of the incoming images
                w: the width of the incoming images
                K: The 3X3 intrinsics matrix
            optimize (bool): Whether to optimize the deep network models for faster inference.
                Still very experimental, and may result in a loss of accuracy with no performance gain!
        """
        self.host = host
        self.port = port
        self.object_dataset = make_object_dataset(mesh_dir)
        model_tuple = self._load_model(model_name)
        self.model_info = model_tuple[0]
        self.model: PoseEstimatesType = model_tuple[1]
        self.model.eval()
        self.camera_data = self._make_camera_data(camera_data)
        self.operations = {
            ServerMessage.GET_POSE.value: self._estimate_pose,
            ServerMessage.GET_VIZ.value: self._raw_viz,
            ServerMessage.SET_INTR.value: self._set_intrinsics,
            ServerMessage.GET_SCORE.value: self._score,
            ServerMessage.SET_SO3_GRID_SIZE.value: self._set_SO3_grid_size,
        }
        print(self.operations.keys())
        self.renderer = Panda3dSceneRenderer(self.object_dataset)
        torch.backends.cudnn.benchmark = True
        torch.backends.cudnn.deterministic = False
        self.optimize = optimize
        if self.optimize:
            class Optimized(nn.Module):
                def delete_weights(self):
                    from torch.nn.utils import prune

                    parameters_to_prune = [
                        (module, "weight") for module in filter(lambda m: type(m) == torch.nn.Conv2d, self.m.modules())
                    ]
                    prune.global_unstructured(
                        parameters_to_prune,
                        pruning_method=prune.L1Unstructured,
                        amount=0.25,
                    )
                    for p in parameters_to_prune:
                        prune.remove(*p)
                        print(list(p[0].named_parameters()))
                def __init__(self, m: nn.Module, inp):
                    super().__init__()
                    self.m = m.eval()
                    self.m = fuse(self.m, inplace=False)
                    # def count_parameters(model): return sum(p.numel() for p in model.parameters())
                    # print(f'Original model has {count_parameters(self.m)} parameters')
                    # self.delete_weights()
                    # print(f'Pruned model has {count_parameters(self.m)} parameters')

                    # self.m = self.m.half().to(memory_format=torch.channels_last)
                    self.m = torch.jit.trace(self.m, torch.rand(inp).cuda())
                    self.m = torch.jit.optimize_for_inference(self.m)


                def forward(self, x):
                    # x = x.to(memory_format=torch.channels_last, dtype=torch.float16)
                    return self.m(x).float()

            class OptimizedTRT(nn.Module):
                def __init__(self, m: nn.Module, inp):
                    super().__init__()
                    import torch_tensorrt
                    inputs = torch_tensorrt.Input(
                        min_shape=inp,
                        opt_shape=inp,
                        max_shape=inp,
                        dtype=torch.half,
                    )
                    enabled_precisions = {torch.float, torch.half}

                    self.m = m.eval()
                    # self.m = self.m.half()
                    self.m = torch_tensorrt.compile(
                        self.m, inputs=inputs, enabled_precisions=enabled_precisions
                    )

                def forward(self, x):
                    x = x.half()
                    return self.m(x).float()
            #fuse_resnet(self.model.coarse_model.backbone)
            h, w = self.camera_data.resolution
            self.model.coarse_model.backbone = Optimized(self.model.coarse_model.backbone, (1, 9, h, w))
            self.model.refiner_model.backbone = Optimized(self.model.refiner_model.backbone, (1, 27, h, w))

            #fuse_resnet(self.model.refiner_model.backbone)
            print(self.model.refiner_model)

        #torch.set_float32_matmul_precision('medium')

    def _load_model(self, model_name):
        return NAMED_MODELS[model_name], load_named_model(model_name, self.object_dataset).cuda()

    def _estimate_pose(self, s: socket.socket, buffer: io.BytesIO):
        '''
        Network method
        Estimate the pose of an object, given an image and a detected region of interest

        the name of the detection that is sent by the client should be in the list of the objects that are in the meshes directory
        the detection bounding box is a 4d array of type [x_min, y_min, x_max, y_max], with values in pixels.
        '''
        import time
        t = time.time()
        img = read_image(buffer)

        json_object = json.loads(read_string(buffer))
        depth = None
        if json_object['use_depth']:
            if not self.model_info['requires_depth']:
                def err(buffer):
                    pack_string('Trying to use depth with a model that cannot handle it', buffer)
                msg = create_message(ServerMessage.ERR, err)
                s.sendall(msg)
                return
            else:
                depth_raw = read_uint16_image(buffer)
                depth = depth_raw * np.float32(json_object['depth_scale_to_m'])
                depth = depth.astype(np.float32)


        labels = json_object['labels']
        detections = json_object.get('detections')
        initial_cTos = json_object.get('initial_cTos')
        coarse_estimates = None
        if initial_cTos is not None:
            cTos_np = np.array(initial_cTos).reshape(-1, 4, 4)
            tensor = torch.from_numpy(cTos_np).float().cuda()
            infos = pd.DataFrame.from_dict({
                'label': labels,
                'batch_im_id': [0 for _ in range(len(cTos_np))],
                'instance_id': [i for i in range(len(cTos_np))]
            })
            coarse_estimates = PoseEstimatesType(infos, poses=tensor)
        # print(f'Input buffer processing took: {time.time() - t}s')
        detections = self._make_detections(labels, detections).cuda() if detections is not None else None
        observation = self._make_observation_tensor(img, depth).cuda()
        inference_params = self.model_info['inference_parameters'].copy()
        if 'refiner_iterations' in json_object:
            inference_params['n_refiner_iterations'] = json_object['refiner_iterations']
        t = time.time()

        output, extra_data = self.model.run_inference_pipeline(
            observation, detections=detections, **inference_params, coarse_estimates=coarse_estimates
        )
        #print(f'Inference took {int((time.time() - t) * 1000.0)}ms')
        # print(extra_data)

        def make_result(buffer):
            poses = output.poses.cpu().numpy()
            poses = poses.reshape(len(poses), 16).tolist()
            scores = output.infos['pose_score'].to_numpy()
            bounding_boxes = extra_data['scoring']['preds'].tensors['boxes_rend'].cpu().numpy().reshape(-1, 4)
            bounding_boxes = bounding_boxes.tolist()
            result = []
            for pose, score, bb in zip(poses, scores, bounding_boxes):
                result.append({
                    'cTo': pose,
                    'score': float(score),
                    'boundingBox': bb
                })
            j = json.dumps(result)
            pack_string(j, buffer)
        msg = create_message(ServerMessage.RET_POSE, make_result)
        s.sendall(msg)


    def _raw_viz(self, s: socket.socket, buffer: io.BytesIO):
        import time
        t = time.time()
        json_object = json.loads(read_string(buffer))
        labels = json_object['labels']
        view_type = json_object['type']
        poses = np.array(json_object['poses']).reshape((-1, 4, 4))
        camera_data = CameraData()
        camera_data.K = self.camera_data.K
        camera_data.resolution = self.camera_data.resolution

        camera_data.TWC = Transform(np.eye(4))
        #camera_data.TWC = Transform((0, 0, 0, 1), [0.0, 0, -2])
        object_datas = []
        for label, pose in zip(labels, poses):
            object_datas.append(ObjectData(label=label, TWO=Transform(pose)))

        if view_type == 'wireframe':
            self.renderer._app.toggleWireframe()
        camera_data, object_datas = convert_scene_observation_to_panda3d(camera_data, object_datas)
        light_datas = [
            Panda3dLightData(
                light_type="ambient",
                color=((1.0, 1.0, 1.0, 1)),
            ),
        ]
        renderings = self.renderer.render_scene(
            object_datas,
            [camera_data],
            light_datas,
            render_depth=False,
            render_binary_mask=False,
            render_normals=False,
            copy_arrays=True,
        )[0]
        if view_type == 'wireframe':
            self.renderer._app.toggleWireframe()
        img = renderings.rgb
        alphas = np.ones((*img.shape[:2], 1), dtype=np.uint8) * 255
        data = np.concatenate((img, alphas), axis=-1)

        msg = create_message(ServerMessage.RET_VIZ, lambda x: pack_image(data, x))
        s.sendall(msg)


    def _score(self, s: socket.socket, buffer: io.BytesIO):
        img = read_image(buffer)
        json_object = json.loads(read_string(buffer))

        labels = json_object['labels']

        poses = json_object.get('cTos')
        pose_estimates = None
        if poses is not None:
            cTos_np = np.array(poses).reshape(-1, 4, 4)
            tensor = torch.from_numpy(cTos_np).float().cuda()
            infos = pd.DataFrame.from_dict({
                'label': labels,
                'batch_im_id': [0 for i in range(len(cTos_np))],
                'instance_id': [i for i in range(len(cTos_np))]
            })
            pose_estimates = PoseEstimatesType(infos, poses=tensor)
        observation = self._make_observation_tensor(img).cuda()
        result = self.model.forward_scoring_model(observation, pose_estimates)
        scores = result[0].infos['pose_score']
        def make_result(buffer):
            j = json.dumps(scores.tolist())
            pack_string(j, buffer)
        msg = create_message(ServerMessage.RET_SCORE, make_result)
        s.sendall(msg)

    def _make_detections(self, labels, detections):
        result = []
        for label, detection in zip(labels, detections):
            o = ObjectData(label)
            o.bbox_modal = detection
            result.append(o)

        return make_detections_from_object_data(result)

    def _make_observation_tensor(self, image: np.ndarray, depth: Optional[np.ndarray]=None) -> ObservationTensor:
        '''
        Create an observation tensor from an image and a potential depth image
        '''
        return ObservationTensor.from_numpy(image, depth, self.camera_data.K)


    def _set_intrinsics(self, s: socket.socket, buffer: io.BytesIO) -> None:
        '''
        Network method
        Set the intrinsics and image resolution to be used by megapose
        After this method is called, the input images should have the dimensions given in the message
        '''
        json_object: Dict = json.loads(read_string(buffer))
        px, py, u0, v0 = [float(x) for x in itemgetter('px', 'py', 'u0', 'v0')(json_object)]
        h, w = [int(x) for x in itemgetter('h', 'w')(json_object)]
        K = np.asarray([
            [px, 0.0, u0],
            [0.0, py, v0],
            [0.0, 0.0, 1.0]
        ])
        self.camera_data = self._make_camera_data({
            'K': K,
            'h': h,
            'w': w
        })
        msg = create_message(ServerMessage.OK, lambda x: None)
        s.sendall(msg)

    def _set_SO3_grid_size(self, s: socket.socket, buffer: io.BytesIO):
        '''
        Set the SO(3) grid size. This dictates the number of images generated to find a coarse pose estimate.
        '''
        json_object = json.loads(read_string(buffer))
        value = json_object['so3_grid_size']
        self.model.load_SO3_grid(value)
        msg = create_message(ServerMessage.OK, lambda x: None)
        s.sendall(msg)


    def _make_camera_data(self, camera_data: Dict) -> CameraData:
        '''
        Create a camera representation that is understandable by megapose.
        camera_data: A dict containing the keys K, h, w
        K is the 3x3 intrinsics matrix
        h and w are the input image resolution.

        Returns a CameraData object, to be given to megapose.
        '''
        c = CameraData()
        c.K = camera_data['K']
        c.resolution = (camera_data['h'], camera_data['w'])
        print(c)
        c.z_near = 0.001
        c.z_far = 100000
        return c


    def run(self):
        """
        Starts the server, listening for incoming connections.
        """
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((self.host, self.port))
                s.listen()
                print('MegaPose server listening for connections')
                while True:
                    connection, address = s.accept()
                    print(f'Connected to {address}')
                    with connection:
                        while True:
                            try:
                                import time
                                t = time.time()
                                code, buffer = receive_message(connection)
                                self.operations[code](connection, buffer)
                            except:
                                traceback.print_exc()
                                print('Connection broken')
                                break
        except Exception as e:
            traceback.print_exc()
            print('Shutting down server')




if __name__ == '__main__':
    megapose_models = {
        'RGB': ('megapose-1.0-RGB', False),
        'RGBD': ('megapose-1.0-RGBD', True),
        'RGB-multi-hypothesis': ('megapose-1.0-RGB-multi-hypothesis', False),
        'RGBD-multi-hypothesis': ('megapose-1.0-RGB-multi-hypothesis-icp', True),
    }
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', type=str, default='127.0.0.1', help='IP or hostname to bind the server to')
    parser.add_argument('--port', type=int, default=5555, help='The port on which to listen for new connections')
    parser.add_argument('--model', type=str, choices=megapose_models.keys(), default='RGB-multi-hypothesis', help='''
                        Which MegaPose model to use. Some models require the depth map.
                        Some models generate multiple hypotheses when estimating the pose, at the cost of more computation.''')
    parser.add_argument('--meshes-directory', type=str, default='./meshes')
    parser.add_argument('--optimize', action='store_true')



    args = parser.parse_args()
    mesh_dir = Path(args.meshes_directory)
    assert mesh_dir.exists(), 'Mesh directory does not exist, cannot start server'
    # Default camera data
    camera_data = {
        'K': np.asarray([
            [700, 0.0, 320],
            [0.0, 700, 240],
            [0.0, 0.0, 1.0]
        ]),
        'h': 480,
        'w': 640
    }

    server = MegaposeServer(args.host, args.port, megapose_models[args.model][0], mesh_dir, camera_data, optimize=args.optimize)

    server.run()





