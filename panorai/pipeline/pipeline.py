from ..projection.projector import deg_to_rad
import numpy as np
from .pipeline_data import PipelineData
from skimage.transform import resize
        
import logging
import os
import sys

# For parallelization
from joblib import Parallel, delayed

from .utils.resizer import ResizerConfig
from ..sampler import SamplerConfig
from ..projection import ProjectorConfig

# Configure logging
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG if os.environ.get('DEBUG', 'False').lower() in ('true', '1') else logging.INFO)

stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setLevel(logger.level)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
stream_handler.setFormatter(formatter)
logger.handlers = [stream_handler]  # Replace existing handlers

class PipelineConfig:
    """Configuration for the pipeline."""
    def __init__(self, resizer_cfg=None, resize_factor=1.0, n_jobs=1):
        """
        Initialize pipeline-level configuration.

        :param resize_factor: Factor by which to resize input images before projection.
        """
        self.resizer_cfg = resizer_cfg or ResizerConfig(resize_factor=resize_factor)
        self.n_jobs = n_jobs

from ..projection.projector import deg_to_rad
import numpy as np
from .pipeline_data import PipelineData
from skimage.transform import resize
import logging
import os
import sys

# Parallel
from joblib import Parallel, delayed
from .utils.resizer import ResizerConfig
from ..sampler import SamplerConfig
from ..projection import ProjectorConfig

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG if os.environ.get('DEBUG', 'False').lower() in ('true', '1') else logging.INFO)

stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setLevel(logger.level)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
stream_handler.setFormatter(formatter)
logger.handlers = [stream_handler]

class PipelineConfig:
    """Configuration for the pipeline."""
    def __init__(self, resizer_cfg=None, resize_factor=1.0, n_jobs=1):
        self.resizer_cfg = resizer_cfg or ResizerConfig(resize_factor=resize_factor)
        self.n_jobs = n_jobs


class ProjectionPipeline:
    """
    Manages sampling and projection strategies using modular configuration objects.
    Stacks all data channels into one multi-channel array for forward/backward operations,
    automatically un-stacks after backward if input was PipelineData.

    Additionally, if stacking is used in forward pass, we override `img_shape` in 
    backward pass to the actual stacked shape, preventing shape mismatches.
    """
    def __init__(
        self,
        projector_cfg: ProjectorConfig = None,
        pipeline_cfg: PipelineConfig = None,
        sampler_cfg: SamplerConfig = None,
    ):
        """
        :param projector_cfg: Configuration for the projector (optional).
        :param pipeline_cfg: PipelineConfig (optional).
        :param sampler_cfg: SamplerConfig (optional).
        """
        # Default configurations
        self.sampler_cfg = sampler_cfg or SamplerConfig(sampler_cls="CubeSampler")
        self.projector_cfg = projector_cfg or ProjectorConfig(dims=(1024, 1024), shadow_angle_deg=30, unsharp=False)
        self.pipeline_cfg = pipeline_cfg or PipelineConfig(resize_factor=1.0)

        # Create sampler, projector, resizer
        self.sampler = self.sampler_cfg.create_sampler()
        self.projector = self.projector_cfg.create_projector()
        self.resizer = self.pipeline_cfg.resizer_cfg.create_resizer()

        # Parallel setting
        self.n_jobs = self.pipeline_cfg.n_jobs

        # For un-stacking after backward:
        self._original_data = None   # the PipelineData if used
        self._keys_order = None      # list of data keys from stack_all
        self._stacked_shape = None   # shape (H, W, total_channels) from forward pass

    def _resize_image(self, img, upsample=True):
        """Resize the input image using the ImageResizer."""
        return self.resizer.resize_image(img, upsample)
    
    def _prepare_data(self, data):
        """
        If data is PipelineData, call data.stack_all() => single (H, W, C), plus keys_order.
        Store references so we can unstack automatically after backward.
        """
        if isinstance(data, PipelineData):
            stacked, keys_order = data.stack_all()
            self._original_data = data
            self._keys_order = keys_order
            return stacked, keys_order
        elif isinstance(data, np.ndarray):
            self._original_data = None
            self._keys_order = None
            return data, None
        else:
            raise TypeError("Data must be either PipelineData or np.ndarray.")

    # === Forward Projection ===
    def project_with_sampler(self, data, fov=(1, 1)):
        """
        Forward projection on a single stacked array for all tangent points.
        Returns { "stacked": { "point_1": arr, ... } }
        """
        if not self.sampler:
            raise ValueError("Sampler is not set.")

        tangent_points = self.sampler.get_tangent_points()
        prepared_data, _ = self._prepare_data(data)
        
        # Store the shape so backward can override
        if isinstance(prepared_data, np.ndarray):
            self._stacked_shape = prepared_data.shape
        else:
            # Should not happen, but just in case
            self._stacked_shape = None

        projections = {"stacked": {}}
        for idx, (lat_deg, lon_deg) in enumerate(tangent_points, start=1):
            lat = deg_to_rad(lat_deg)
            lon = deg_to_rad(lon_deg)
            logger.debug(f"Forward projecting for point {idx}, lat={lat_deg}, lon={lon_deg}.")
            out_img = self.projector.forward(prepared_data, lat, lon, fov)
            projections["stacked"][f"point_{idx}"] = out_img

        return projections

    def single_projection(self, data, lat_center, lon_center, fov=(1, 1)):
        """
        Single forward projection of a stacked array.
        """
        lat_center = deg_to_rad(lat_center)
        lon_center = deg_to_rad(lon_center)
        prepared_data, _ = self._prepare_data(data)
        if isinstance(prepared_data, np.ndarray):
            self._stacked_shape = prepared_data.shape
        out_img = self.projector.forward(prepared_data, lat_center, lon_center, fov)
        return out_img

    # === Backward Projection ===
    def backward_with_sampler(self, rect_data, img_shape, fov=(1, 1)):
        """
        If _stacked_shape is set from forward pass, override user-supplied `img_shape`
        to avoid shape mismatch. Then do the multi-channel backward pass, unstack if needed.

        :param rect_data: { "stacked": { "point_1": arr, ... } }
        :param img_shape: Potentially (H, W, 3) from user, but if we stacked 7 channels,
                          we override with (H, W, 7).
        :return: 
          If PipelineData was used, returns unstacked dict of { "rgb": arr, "depth": arr, ... }
          If user input was a raw array, returns { "stacked": combined }
        """
        if not self.sampler:
            raise ValueError("Sampler is not set.")

        # If we have a stacked_shape from forward, override
        if self._stacked_shape is not None:
            if img_shape != self._stacked_shape:
                logger.warning(
                    f"Overriding user-supplied img_shape={img_shape} with stacked_shape={self._stacked_shape} "
                    "to ensure consistent channel dimensions."
                )
            img_shape = self._stacked_shape

        tangent_points = self.sampler.get_tangent_points()
        combined = np.zeros(img_shape, dtype=np.float32)
        weight_map = np.zeros(img_shape[:2], dtype=np.float32)

        stacked_dict = rect_data.get("stacked")
        if stacked_dict is None:
            raise ValueError("rect_data must have a 'stacked' key with tangent-point images.")

        tasks = []
        for idx, (lat_deg, lon_deg) in enumerate(tangent_points, start=1):
            rect_img = stacked_dict.get(f"point_{idx}")
            if rect_img is None:
                raise ValueError(f"Missing 'point_{idx}' in rect_data['stacked'].")

            if rect_img.shape[-1] != img_shape[-1]:
                raise ValueError(
                    f"rect_img has {rect_img.shape[-1]} channels, but final shape indicates {img_shape[-1]} channels.\n"
                    "Make sure the shapes match."
                )
            tasks.append((idx, lat_deg, lon_deg, rect_img))

        def _backward_task(idx, lat_deg, lon_deg, rect_img):
            logger.debug(f"[Parallel] Backward projecting point_{idx}, lat={lat_deg}, lon={lon_deg}...")
            lat = deg_to_rad(lat_deg)
            lon = deg_to_rad(lon_deg)
            equirect_img, mask = self.projector.backward(rect_img, img_shape, lat, lon, fov, return_mask=True)
            return idx, equirect_img, mask

        logger.info(f"Starting backward with n_jobs={self.n_jobs} on {len(tasks)} tasks.")
        results = Parallel(n_jobs=self.n_jobs)(
            delayed(_backward_task)(*task) for task in tasks
        )
        logger.info("All backward tasks completed.")

        # Merge
        for (idx, eq_img, mask) in results:
            combined += eq_img * mask[..., None]
            weight_map += mask

        # Optionally blend
        # w = np.maximum(weight_map, 1e-9)
        # combined /= w[..., None]

        # If we had PipelineData, unstack
        if self._original_data is not None and self._keys_order is not None:
            new_data = self._original_data.unstack_new_instance(combined, self._keys_order)
            return new_data.as_dict()
        else:
            return {"stacked": combined}

    def single_backward(self, rect_data, img_shape, lat_center, lon_center, fov=(1, 1)):
        """
        If we have self._stacked_shape, override user-supplied shape for channel consistency.
        If pipeline data was used, unstack automatically.
        """
        lat_center = deg_to_rad(lat_center)
        lon_center = deg_to_rad(lon_center)

        if self._stacked_shape is not None and img_shape != self._stacked_shape:
            logger.warning(
                f"Overriding user-supplied img_shape={img_shape} with stacked_shape={self._stacked_shape} "
                "for single_backward."
            )
            img_shape = self._stacked_shape

        if isinstance(rect_data, np.ndarray):
            out_img, _ = self.projector.backward(rect_data, img_shape, lat_center, lon_center, fov, return_mask=True)
            if self._original_data is not None and self._keys_order is not None:
                new_data = self._original_data.unstack_new_instance(out_img, self._keys_order)
                return new_data.as_dict()
            else:
                return out_img
        else:
            # Must have "stacked" key
            stacked_arr = rect_data.get("stacked")
            if stacked_arr is None:
                raise ValueError("Expecting key 'stacked' in rect_data for single_backward.")

            if stacked_arr.shape[-1] != img_shape[-1]:
                raise ValueError(
                    f"Stacked array has {stacked_arr.shape[-1]} channels, but final shape indicates {img_shape[-1]}.\n"
                    "Shapes must match."
                )
            out_img, _ = self.projector.backward(stacked_arr, img_shape, lat_center, lon_center, fov, return_mask=True)
            if self._original_data is not None and self._keys_order is not None:
                new_data = self._original_data.unstack_new_instance(out_img, self._keys_order)
                return new_data.as_dict()
            else:
                return out_img

class __ProjectionPipeline:
    """
    Manages sampling and projection strategies using modular configuration objects.
    Stacks all data channels into one multi-channel array for forward/backward operations,
    and un-stacks them automatically after backward pass if input was PipelineData.
    """
    def __init__(
        self,
        projector_cfg: ProjectorConfig = None,
        pipeline_cfg: PipelineConfig = None,
        sampler_cfg: SamplerConfig = None,
    ):
        """
        :param projector_cfg: Configuration for the projector (optional).
        :param pipeline_cfg: PipelineConfig (optional).
        :param sampler_cfg: SamplerConfig (optional).
        """
        # Default configurations
        self.sampler_cfg = sampler_cfg or SamplerConfig(sampler_cls="CubeSampler")
        self.projector_cfg = projector_cfg or ProjectorConfig(dims=(1024, 1024), shadow_angle_deg=30, unsharp=False)
        self.pipeline_cfg = pipeline_cfg or PipelineConfig(resize_factor=1.0)

        # Create sampler, projector, resizer
        self.sampler = self.sampler_cfg.create_sampler()
        self.projector = self.projector_cfg.create_projector()
        self.resizer = self.pipeline_cfg.resizer_cfg.create_resizer()

        # Parallel setting
        self.n_jobs = self.pipeline_cfg.n_jobs

        # We store references to handle unstacking automatically
        self._original_data = None  # Will store the actual PipelineData object if used
        self._keys_order = None     # Will store the list of channel keys from stack_all

    def _resize_image(self, img, upsample=True):
        """Resize the input image using the ImageResizer."""
        return self.resizer.resize_image(img, upsample)
    
    def _prepare_data(self, data):
        """
        If data is a PipelineData, call data.stack_all() => returns a single (H, W, C) array + key order.
        If it's already a np.ndarray, return it as-is, with None for keys_order.
        Also store references for unstacking later if PipelineData is used.
        """
        if isinstance(data, PipelineData):
            stacked, keys_order = data.stack_all()
            self._original_data = data       # so we can unstack into the same data object if we want
            self._keys_order = keys_order
            return stacked, keys_order
        elif isinstance(data, np.ndarray):
            # No stacking needed
            self._original_data = None
            self._keys_order = None
            return data, None
        else:
            raise TypeError("Data must be either PipelineData or np.ndarray.")

    # === Forward Projection ===
    def project_with_sampler(self, data, fov=(1, 1)):
        """
        Perform forward projection on a single stacked array for all tangent points.
        Returns a dict like:
          {
            "stacked": {
              "point_1": <arr>,
              "point_2": <arr>,
              ...
            }
          }
        or multiple keys if you prefer that structure. Here we unify to "stacked".
        """
        if not self.sampler:
            raise ValueError("Sampler is not set.")

        tangent_points = self.sampler.get_tangent_points()
        prepared_data, _ = self._prepare_data(data)

        projections = {"stacked": {}}

        for idx, (lat_deg, lon_deg) in enumerate(tangent_points, start=1):
            lat = deg_to_rad(lat_deg)
            lon = deg_to_rad(lon_deg)
            logger.debug(f"Forward projecting stacked data for point {idx}, lat={lat_deg}, lon={lon_deg}.")
            out_img = self.projector.forward(prepared_data, lat, lon, fov)
            projections["stacked"][f"point_{idx}"] = out_img

        return projections

    def single_projection(self, data, lat_center, lon_center, fov=(1, 1)):
        """
        Single forward projection of a stacked array.
        """
        lat_center = deg_to_rad(lat_center)
        lon_center = deg_to_rad(lon_center)
        prepared_data, _ = self._prepare_data(data)
        out_img = self.projector.forward(prepared_data, lat_center, lon_center, fov)
        return out_img

    # === Backward Projection ===
    def backward_with_sampler(self, rect_data, img_shape, fov=(1, 1)):
        """
        Perform backward projection on a single stacked array for all tangent points.
        rect_data must be: { "stacked": { "point_1": <arr>, ... } }
        :param img_shape: (H, W, C) total shape, matching the # of stacked channels.
        :return: 
          - If input was PipelineData, returns a dictionary of unstacked arrays 
            (the same keys as original PipelineData).
          - If input was raw np.ndarray, returns { "stacked": combined } as before.
        """
        if not self.sampler:
            raise ValueError("Sampler is not set.")

        tangent_points = self.sampler.get_tangent_points()

        combined = np.zeros(img_shape, dtype=np.float32)
        weight_map = np.zeros(img_shape[:2], dtype=np.float32)

        # Prepare tasks for parallel
        tasks = []
        stacked_dict = rect_data.get("stacked")
        if stacked_dict is None:
            raise ValueError("rect_data must have a 'stacked' key with tangent-point images.")

        for idx, (lat_deg, lon_deg) in enumerate(tangent_points, start=1):
            rect_img = stacked_dict.get(f"point_{idx}")
            if rect_img is None:
                raise ValueError(f"Missing 'point_{idx}' in rect_data['stacked'].")

            # shape check
            if rect_img.shape[-1] != img_shape[-1]:
                raise ValueError(
                    f"rect_img has {rect_img.shape[-1]} channels, but img_shape indicates {img_shape[-1]} channels.\n"
                    "Make sure you pass the stacked shape (H, W, total_channels)."
                )

            tasks.append((idx, lat_deg, lon_deg, rect_img))

        def _backward_task(idx, lat_deg, lon_deg, rect_img):
            logger.debug(f"[Parallel] Backward projecting point_{idx}, lat={lat_deg}, lon={lon_deg}...")
            lat = deg_to_rad(lat_deg)
            lon = deg_to_rad(lon_deg)
            equirect_img, mask = self.projector.backward(rect_img, img_shape, lat, lon, fov, return_mask=True)
            return idx, equirect_img, mask

        logger.info(f"Starting backward with n_jobs={self.n_jobs} on {len(tasks)} tasks.")
        results = Parallel(n_jobs=self.n_jobs)(
            delayed(_backward_task)(*task) for task in tasks
        )
        logger.info("All backward tasks completed.")

        # Merge
        for (idx, eq_img, mask) in results:
            combined += eq_img * mask[..., None]
            weight_map += mask

        # Optional blending
        # w = np.maximum(weight_map, 1e-9)
        # combined /= w[..., None]

        # === AUTO-UNSTACK if we have pipeline data ===
        if self._original_data is not None and self._keys_order is not None:
            # Create a new PipelineData from the original
            # unstack the combined array into the original keys
            new_data = self._original_data.unstack_new_instance(combined, self._keys_order)
            return new_data.as_dict()  # returns dict of { "rgb": <arr>, "depth": <arr>, ... }

        else:
            # If we didn't store an original PipelineData (i.e. user passed np.ndarray),
            # then we just return { "stacked": combined } for consistency.
            return {"stacked": combined}

    def single_backward(self, rect_data, img_shape, lat_center, lon_center, fov=(1, 1)):
        """
        Single backward pass for stacked data. 
        If we had PipelineData originally, we unstack before returning.
        """
        lat_center = deg_to_rad(lat_center)
        lon_center = deg_to_rad(lon_center)

        if isinstance(rect_data, np.ndarray):
            # Just a single array
            out_img, _ = self.projector.backward(rect_data, img_shape, lat_center, lon_center, fov, return_mask=True)
            # If we had pipeline data, let's also unstack. But single_backward isn't typically used that way.
            if self._original_data is not None and self._keys_order is not None:
                # unstack it
                new_data = self._original_data.unstack_new_instance(out_img, self._keys_order)
                return new_data.as_dict()
            else:
                return out_img

        # else rect_data is a dict with "stacked"
        stacked_arr = rect_data.get("stacked")
        if stacked_arr is None:
            raise ValueError("Expecting key 'stacked' in rect_data for single_backward.")
        if stacked_arr.shape[-1] != img_shape[-1]:
            raise ValueError(
                f"Stacked array has {stacked_arr.shape[-1]} channels, but expected {img_shape[-1]}.\n"
                "Pass the correct stacked shape."
            )
        out_img, _ = self.projector.backward(stacked_arr, img_shape, lat_center, lon_center, fov, return_mask=True)

        # unstack if pipeline data
        if self._original_data is not None and self._keys_order is not None:
            new_data = self._original_data.unstack_new_instance(out_img, self._keys_order)
            return new_data.as_dict()
        else:
            return out_img
        

class _ProjectionPipeline:
    """
    Manages sampling and projection strategies using modular configuration objects.
    """
    def __init__(
        self,
        projector_cfg: ProjectorConfig = None,
        pipeline_cfg: PipelineConfig = None,
        sampler_cfg: SamplerConfig = None,
    ):
        """
        Initialize the pipeline with configuration objects.

        :param sampler_cfg: Configuration for the sampler (optional).
        :param projector_cfg: Configuration for the projector (optional).
        :param pipeline_cfg: Configuration for the pipeline (optional).
        :param n_jobs: Number of parallel jobs to use during backward projection.
                       1 means no parallelism; >1 uses multiprocessing via joblib.
        """
        # Use default configurations if not provided
        self.sampler_cfg = sampler_cfg or SamplerConfig(sampler_cls="CubeSampler")
        self.projector_cfg = projector_cfg or ProjectorConfig(dims=(1024, 1024), shadow_angle_deg=30, unsharp=False)
        self.pipeline_cfg = pipeline_cfg or PipelineConfig(resize_factor=1.0)

        # Initialize sampler and projector
        self.sampler = self.sampler_cfg.create_sampler()
        self.projector = self.projector_cfg.create_projector()
        self.resizer = self.pipeline_cfg.resizer_cfg.create_resizer()

        # Keep the parallel setting
        self.n_jobs = self.pipeline_cfg.n_jobs

    def _resize_image(self, img, upsample=True):
        """Resize the input image using the ImageResizer."""
        return self.resizer.resize_image(img, upsample)
    
    def _prepare_data(self, data):
        """
        Convert the input into a dict of {data_name: image_array}, applying resize if needed.
        """
        if isinstance(data, PipelineData):
            return {k: self._resize_image(v) for k, v in data.as_dict().items()}
        elif isinstance(data, np.ndarray):
            return {"rgb": self._resize_image(data)}
        else:
            raise TypeError("Data must be either a PipelineData instance or a NumPy array.")

    def set_sampler(self, sampler):
        """Set the sphere sampler."""
        self.sampler = sampler

    def set_projector(self, projector):
        """Set the projection strategy."""
        self.projector = projector

    # === Forward Projections ===
    def project_with_sampler(self, data, fov=(1, 1)):
        """
        Perform forward projection for all tangent points in the sampler.
        """
        if not self.sampler:
            raise ValueError("Sampler is not set.")

        tangent_points = self.sampler.get_tangent_points()
        prepared_data = self._prepare_data(data)
        projections = {name: {} for name in prepared_data.keys()}

        for idx, (lat_deg, lon_deg) in enumerate(tangent_points):
            lat = deg_to_rad(lat_deg)
            lon = deg_to_rad(lon_deg)
            for name, img in prepared_data.items():
                logger.debug(f"Projecting {name} for tangent point {idx+1} (lat={lat_deg}, lon={lon_deg}).")
                projections[name][f"point_{idx + 1}"] = self.projector.forward(img, lat, lon, fov)

        return projections

    def single_projection(self, data, lat_center, lon_center, fov=(1, 1)):
        """
        Perform a single forward projection for multiple inputs.
        """
        lat_center = deg_to_rad(lat_center)
        lon_center = deg_to_rad(lon_center)
        prepared_data = self._prepare_data(data)
        projections = {
            name: self.projector.forward(img, lat_center, lon_center, fov)
            for name, img in prepared_data.items()
        }

        # If input was a single image, return only that result
        if isinstance(data, np.ndarray):
            return list(projections.values())[0]
        return projections

    # === Backward Projections ===
    def backward_with_sampler(self, rect_data, img_shape, fov=(1, 1)):
        """
        Perform backward projection for all tangent points in the sampler and 
        combine results into a single equirectangular image.

        :param rect_data: Dictionary of rectilinear images {data_name: {point_{n}: image}}
        :param img_shape: Shape of the original spherical image (H, W, C).
        :param fov: Field of view (height, width).
        :return: Dictionary of reconstructed equirectangular images {data_name: image}.
        """
        if not self.sampler:
            raise ValueError("Sampler is not set.")

        tangent_points = self.sampler.get_tangent_points()

        # Initialize final combined images + weight maps
        combined_images = {
            name: np.zeros(img_shape, dtype=np.float32)
            for name in rect_data.keys()
        }
        weight_map = {
            name: np.zeros(img_shape[:2], dtype=np.float32)
            for name in rect_data.keys()
        }

        # Collect tasks in a list to run in parallel
        # For each tangent point and each data channel, we do a backward call
        tasks = []
        for idx, (lat_deg, lon_deg) in enumerate(tangent_points, start=1):
            lat = deg_to_rad(lat_deg)
            lon = deg_to_rad(lon_deg)

            for name, images_dict in rect_data.items():
                rect_img = images_dict.get(f"point_{idx}")
                if rect_img is None:
                    raise ValueError(f"Missing projection for point_{idx} in rect_data[{name}].")
                tasks.append((name, idx, lat, lon, rect_img))

        # We'll define a helper function for parallel execution
        def _process_backward_task(name, idx, lat, lon, rect_img):
            """
            1) Runs projector.backward
            2) Returns the resulting equirect_img, mask, and name, idx for merging.
            """
            logger.debug(f"[Parallel] Backward projecting {name} point_{idx}, lat={lat}, lon={lon}...")
            equirect_img, mask = self.projector.backward(rect_img, img_shape, lat, lon, fov, return_mask=True)
            return (name, idx, equirect_img, mask)

        logger.info(f"Starting backward projection with n_jobs={self.n_jobs} across {len(tasks)} tasks.")
        results = Parallel(n_jobs=self.n_jobs)(
            delayed(_process_backward_task)(*task) for task in tasks
        )
        logger.info("All backward projections completed in parallel.")

        #  Merge results into combined_images
        for (name, idx, equirect_img, mask) in results:
            combined_images[name] += equirect_img * mask[..., None]
            weight_map[name] += mask

        # Optional: Normalize by weight map if you'd like blending
        # for name in combined_images.keys():
        #     w = np.maximum(weight_map[name], 1e-9)
        #     combined_images[name] /= w[..., None]

        return combined_images

    def single_backward(self, rect_data, img_shape, lat_center, lon_center, fov=(1, 1)):
        """
        Perform a single backward projection for multiple data channels or a single image.
        """
        lat_center = deg_to_rad(lat_center)
        lon_center = deg_to_rad(lon_center)

        if isinstance(rect_data, np.ndarray):
            rect_data = {"rgb": rect_data}

        projections = {}
        for name, img in rect_data.items():
            projections[name] = self.projector.backward(img, img_shape, lat_center, lon_center, fov)

        # If only one item 'rgb', return it as array
        if len(rect_data) == 1 and "rgb" in rect_data:
            return list(projections.values())[0]
        return projections