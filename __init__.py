import bpy
import math
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tempfile
import time


class Globals:
    active_render_processes = []
    is_rendering_active = False
    is_benchmarking = False
    early_exit_benchmark = False
    bench_status_msg = ""
    current_bench_instances = 1
    benchmark_frames = 1
    benchmark_results = {}
    bench_temp_dir = ""
    start_time = 0
    elapsed_time = 0
    seconds_per_frame = 0
    declining_streak = 0
    peak_throughput = 0

    # Progress tracking
    frames_done = 0
    frames_total = 0
    eta_seconds = 0
    snapshot_frames = set()  # frames that existed before render started

    gpu_detection_active = False
    userpref_process = None
    userpref_path = ""

    dummy_scene_process = None

    gpu_discovery_processes = []

    gpu_devices = []
    gpu_devices_envs = []
    gpu_detected = False
    gpu_configured = False
    gpu_config_dir = ""


def format_time(seconds):
    """Formats seconds into a human-readable string like 1ч 23м 45с."""
    seconds = int(seconds)
    h, remainder = divmod(seconds, 3600)
    m, s = divmod(remainder, 60)
    if h > 0:
        return f"{h}h {m:02d}m {s:02d}s"
    elif m > 0:
        return f"{m}m {s:02d}s"
    else:
        return f"{s}s"


def get_frame_number_from_filename(filename, output_prefix):
    """Extracts frame number from a rendered filename.
    Blender names files like 'prefix0001.png', 'prefix0042.exr', etc.
    Returns the frame number as int, or None if not recognized."""
    basename = os.path.basename(filename)
    name_no_ext = os.path.splitext(basename)[0]

    # Strip the output prefix (e.g. 'frame_' from 'frame_0001')
    prefix_basename = os.path.basename(output_prefix.rstrip("/\\"))
    if prefix_basename and name_no_ext.startswith(prefix_basename):
        remainder = name_no_ext[len(prefix_basename):]
    else:
        remainder = name_no_ext

    # The remainder should be a zero-padded number
    if remainder.isdigit():
        return int(remainder)
    return None


def scan_output_folder(frame_start, frame_end, output_prefix):
    """Scans the render output folder and returns info about existing frames.

    Returns a dict with:
      - 'valid': set of frame numbers that are complete and valid
      - 'total_expected': total number of frames in the range
      - 'output_dir': the directory that was scanned
    """
    output_dir = os.path.dirname(bpy.path.abspath(output_prefix))
    result = {
        "valid": set(),
        "total_expected": frame_end - frame_start + 1,
        "output_dir": output_dir,
    }

    if not os.path.exists(output_dir):
        return result

    for filename in os.listdir(output_dir):
        file_path = os.path.join(output_dir, filename)
        if not os.path.isfile(file_path):
            continue
        frame_num = get_frame_number_from_filename(filename, output_prefix)
        if frame_num is None:
            continue
        if frame_start <= frame_num <= frame_end:
            if is_image_valid(file_path):
                result["valid"].add(frame_num)

    return result


def is_image_valid(filepath):
    """Checks if an image file is complete by looking for format-specific footers."""
    if not os.path.exists(filepath) or os.path.getsize(filepath) == 0:
        return False

    ext = os.path.splitext(filepath)[1].lower()

    try:
        with open(filepath, "rb") as f:
            f.seek(-10, 2)
            footer = f.read()

            if ext == ".png":
                return b"\xaeB`\x82" in footer
            elif ext in {".jpg", ".jpeg"}:
                return b"\xff\xd9" in footer
            elif ext == ".exr":
                return os.path.getsize(filepath) > 1000
    except Exception as e:
        print(f"PseudoRenderingFarmEX: Error checking {filepath}: {e}")
        return False

    return True


def cleanup_corrupted_frames():
    scene = bpy.context.scene
    output_path = bpy.path.abspath(scene.render.filepath)
    output_dir = os.path.dirname(output_path)

    if not os.path.exists(output_dir):
        return

    deleted_count = 0
    for filename in os.listdir(output_dir):
        file_path = os.path.join(output_dir, filename)
        if os.path.isfile(file_path):
            if not is_image_valid(file_path):
                try:
                    os.remove(file_path)
                    deleted_count += 1
                except Exception as e:
                    print(f"PseudoRenderingFarmEX: Failed to delete {filename}: {e}")

    return deleted_count


def cleanup_bench_dir():
    if Globals.bench_temp_dir and os.path.exists(Globals.bench_temp_dir):
        try:
            shutil.rmtree(Globals.bench_temp_dir)
        except Exception:
            pass
        Globals.bench_temp_dir = ""


def terminate_all_processes():
    for proc in Globals.active_render_processes:
        if proc.poll() is None:
            proc.terminate()
    for proc in Globals.active_render_processes:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    Globals.active_render_processes.clear()


def update_render_progress(scene):
    """Counts completed frames and updates ETA. Called every timer tick."""
    # Use the effective range stored at render start (respects custom range)
    eff_total = Globals.frames_total
    if eff_total == 0:
        return

    output_prefix = scene.render.filepath
    # Derive eff_start/eff_end from snapshot context stored in frames_total
    # We scan the full scene range but filter by snapshot delta
    scan = scan_output_folder(
        scene.frame_start, scene.frame_end, output_prefix
    )

    # Subtract frames that already existed before render started
    new_frames = scan["valid"] - Globals.snapshot_frames
    Globals.frames_done = len(new_frames)

    elapsed = time.time() - Globals.start_time
    remaining = eff_total - Globals.frames_done

    if elapsed > 3 and Globals.frames_done > 0:
        avg_spf = elapsed / Globals.frames_done  # seconds per frame
        Globals.eta_seconds = remaining * avg_spf
    else:
        Globals.eta_seconds = 0


def check_render_status():
    for proc in Globals.active_render_processes[:]:
        if proc.poll() is not None:
            Globals.active_render_processes.remove(proc)

    if Globals.early_exit_benchmark or (
        Globals.is_benchmarking and not Globals.active_render_processes
    ):
        elapsed = time.time() - Globals.start_time
        throughput = Globals.benchmark_frames / max(elapsed, 0.001)

        Globals.benchmark_results[Globals.current_bench_instances] = throughput

        if throughput > Globals.peak_throughput:
            Globals.peak_throughput = throughput
            Globals.declining_streak = 0
        else:
            Globals.declining_streak += 1

        if (
            Globals.early_exit_benchmark
            or Globals.current_bench_instances >= 16
            or Globals.declining_streak >= 2 * max(len(Globals.gpu_devices), 1)
        ):
            best_count = max(
                Globals.benchmark_results, key=Globals.benchmark_results.get
            )
            bpy.context.scene.pseudo_rendering_farm_instances = best_count

            Globals.is_benchmarking = False
            Globals.early_exit_benchmark = False
            Globals.bench_status_msg = f"Optimal found: {best_count}"
            cleanup_bench_dir()

            def draw_popup(self, context):
                self.layout.label(
                    text=f"Benchmark is complete, optimal number of instances is {best_count} with {1.0 / Globals.benchmark_results[best_count]:.1f} seconds per frame",
                    icon="CHECKMARK",
                )

            print("PseudoRenderingFarmEX: !!! Benchmarking stats for nerds !!!")
            print(Globals.benchmark_results)

            if not bpy.app.background:
                bpy.context.window_manager.popup_menu(
                    draw_popup, title="Benchmark Complete", icon="RENDER_RESULT"
                )

                for window in bpy.context.window_manager.windows:
                    for area in window.screen.areas:
                        area.tag_redraw()

            return None
        else:
            Globals.current_bench_instances += 1
            for window in bpy.context.window_manager.windows:
                for area in window.screen.areas:
                    area.tag_redraw()
            launch_benchmark_iteration(bpy.context)

    if Globals.is_rendering_active:
        update_render_progress(bpy.context.scene)
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                area.tag_redraw()

        # Adaptive polling: check more often at the start,
        # slow down after 2 minutes to reduce disk reads
        elapsed = time.time() - Globals.start_time
        if elapsed < 120:
            poll_interval = 2.0
        else:
            poll_interval = 5.0
    else:
        poll_interval = 1.0

    if Globals.is_rendering_active and not Globals.active_render_processes:

        Globals.elapsed_time = time.time() - Globals.start_time
        scene = bpy.context.scene
        frames = Globals.frames_total or (scene.frame_end - scene.frame_start + 1)
        Globals.seconds_per_frame = Globals.elapsed_time / frames if frames else 0
        Globals.is_rendering_active = False
        Globals.eta_seconds = 0

        def draw_popup(self, context):
            self.layout.label(
                text=f"All instances finished in {Globals.elapsed_time:.1f} s at {Globals.seconds_per_frame:.1f} seconds per frame",
                icon="CHECKMARK",
            )

        if not bpy.app.background:
            bpy.context.window_manager.popup_menu(
                draw_popup,
                title="Pseudo Rendering Farm Complete",
                icon="RENDER_ANIMATION",
            )
            for window in bpy.context.window_manager.windows:
                for area in window.screen.areas:
                    area.tag_redraw()

        return None

    return poll_interval


def check_multi_gpu_status():
    setup_multi_gpu()
    if Globals.gpu_configured:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                area.tag_redraw()
        return None

    return 1.0


def detect_gpus():
    if Globals.gpu_detected:
        return
    Globals.gpu_detected = True
    Globals.gpu_devices_envs = [os.environ.copy()]

    try:
        if (
            platform.system() != "Darwin"
            and "VULKAN" not in bpy.context.preferences.system.gpu_backend
        ):
            print("PseudoRenderingFarmEX: Non-Vulkan backend, multi-GPU not available")
            return
        try:
            bpy.context.preferences.system.gpu_preferred_device = "___invalid___"
        except TypeError as e:
            Globals.gpu_devices = [
                d for d in re.findall(r"'([^']+)'", str(e)) if d != "AUTO"
            ]
    except Exception as e:
        print(f"PseudoRenderingFarmEX: GPU detection failed: {e}")


def setup_multi_gpu():
    if not Globals.userpref_path:
        if Globals.userpref_process is None:
            expr = "import bpy, os; print('USERPREF:' + os.path.join(bpy.utils.resource_path('USER'), 'config', 'userpref.blend'))"

            Globals.userpref_process = subprocess.Popen(
                [bpy.app.binary_path, "-b", "--python-expr", expr],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            return
        status = Globals.userpref_process.poll()
        if status is not None:
            stdout, stderr = Globals.userpref_process.communicate()

            for line in (stdout + stderr).splitlines():
                if line.startswith("USERPREF:"):
                    Globals.userpref_path = line[len("USERPREF:") :]
                    return

    if not Globals.gpu_config_dir:
        Globals.gpu_config_dir = tempfile.mkdtemp(prefix="gpu_config_")
    scene_path = os.path.join(Globals.gpu_config_dir, "temp_scene.blend")

    if not os.path.isfile(scene_path):
        if Globals.dummy_scene_process is None:
            expr = f"import bpy; bpy.ops.wm.read_homefile(); bpy.ops.wm.save_as_mainfile(filepath=r'{scene_path}')"

            Globals.dummy_scene_process = subprocess.Popen(
                [bpy.app.binary_path, "-b", "--python-expr", expr],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            return
        status = Globals.dummy_scene_process.poll()
        if status is None:
            return

    if not Globals.gpu_discovery_processes:
        Globals.gpu_devices_envs = []
        for i, gpu_name in enumerate(Globals.gpu_devices):
            gpu_dir = os.path.join(Globals.gpu_config_dir, f"gpu_{i}")
            os.makedirs(gpu_dir, exist_ok=True)
            shutil.copy2(Globals.userpref_path, os.path.join(gpu_dir, "userpref.blend"))

            env = os.environ.copy()
            env["BLENDER_USER_CONFIG"] = gpu_dir

            cmd = [
                bpy.app.binary_path,
                scene_path,
                "--python-expr",
                f"import bpy; bpy.context.preferences.system.gpu_preferred_device = '{gpu_name}'; bpy.ops.wm.save_userpref(); bpy.ops.wm.quit_blender()",
            ]
            Globals.gpu_discovery_processes.append(
                subprocess.Popen(
                    cmd,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )

            Globals.gpu_devices_envs.append(env)

        if not Globals.gpu_devices_envs:
            Globals.gpu_devices_envs = [os.environ.copy()]
            return

    if all(p.poll() is not None for p in Globals.gpu_discovery_processes):
        Globals.gpu_configured = True
    return


def cleanup_gpu_config():
    if Globals.gpu_config_dir and os.path.exists(Globals.gpu_config_dir):
        try:
            shutil.rmtree(Globals.gpu_config_dir)
        except Exception:
            pass
        Globals.gpu_config_dir = ""


def get_env_for_instance(index):
    if not Globals.gpu_devices_envs:
        Globals.gpu_devices_envs = [os.environ.copy()]
    return Globals.gpu_devices_envs[index % len(Globals.gpu_devices_envs)]


def get_process_priority_kwargs():
    """Returns kwargs for subprocess.Popen to launch with below-normal CPU priority.
    GPU scheduling is independent so render speed is not affected."""
    if platform.system() == "Windows":
        # BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
        return {"creationflags": 0x00004000}
    else:
        # Unix: preexec_fn to renice the process
        return {"preexec_fn": lambda: os.nice(10)}


def get_worker_subrange(start_idx, end_idx, num_workers, worker_id):
    total_elements = end_idx - start_idx + 1

    if total_elements <= 0 or worker_id >= num_workers:
        return None

    chunk_size = math.ceil(total_elements / num_workers)
    sub_start = start_idx + (worker_id * chunk_size)
    sub_end = sub_start + chunk_size - 1
    if sub_start > end_idx:
        return None
    sub_end = min(sub_end, end_idx)
    return (sub_start, sub_end)


def using_same_gpus():
    # "10de/2783/0" -> vendor_id/device_id/device_num
    if not Globals.gpu_devices:
        return True
    prefixes = {s.rsplit("/", 1)[0] for s in Globals.gpu_devices}

    return len(prefixes) <= 1


def is_system_balanced():
    return len(Globals.gpu_devices) <= 1 or using_same_gpus()


def is_scene_configured(extension_obj, scene_render):
    if is_system_balanced():
        return True

    if scene_render.use_overwrite:
        extension_obj.report(
            {"ERROR"},
            "Validation Failed: 'Overwrite' must be UNCHECKED for multi-GPU systems with different GPUs",
        )
        return False
    if not scene_render.use_placeholder:
        extension_obj.report(
            {"ERROR"},
            "Validation Failed: 'Placeholders' must be CHECKED for multi-GPU systems with different GPUs",
        )
        return False
    return True


# --- Rendering ---


class RENDER_OT_pseudo_rendering_farm(bpy.types.Operator):
    """Launch multiple background render instances based on current scene settings"""

    bl_idname = "render.pseudo_rendering_farm"
    bl_label = "Launch Pseudo Rendering Farm"

    # Populated in invoke() when existing frames are found in range
    _frames_to_overwrite: int = 0

    def invoke(self, context, event):
        scene = context.scene

        if not bpy.data.filepath:
            self.report({"ERROR"}, "Please save the scene")
            return {"CANCELLED"}

        # Resolve effective range early so we can scan
        if scene.prf_use_custom_range:
            eff_start = scene.prf_frame_start
            eff_end = scene.prf_frame_end
            if eff_start > eff_end:
                self.report({"ERROR"}, "Custom Start frame must be <= End frame")
                return {"CANCELLED"}
        else:
            eff_start = scene.frame_start
            eff_end = scene.frame_end

        # Count existing valid frames in the target range
        scan = scan_output_folder(eff_start, eff_end, scene.render.filepath)
        existing = scan["valid"]
        self._frames_to_overwrite = len(existing)

        if self._frames_to_overwrite > 0:
            # Show confirmation dialog
            return context.window_manager.invoke_props_dialog(
                self, width=420, title="Overwrite Warning"
            )

        # Nothing to overwrite — launch immediately
        return self.execute(context)

    def draw(self, context):
        """Content of the confirmation dialog."""
        scene = context.scene
        if scene.prf_use_custom_range:
            eff_start = scene.prf_frame_start
            eff_end = scene.prf_frame_end
        else:
            eff_start = scene.frame_start
            eff_end = scene.frame_end

        layout = self.layout
        layout.label(
            text=f"{self._frames_to_overwrite} rendered frame(s) in range "
                 f"{eff_start}–{eff_end} will be overwritten.",
            icon="ERROR",
        )
        layout.label(text="Press OK to continue or Cancel to abort.")

    def execute(self, context):
        scene = context.scene

        if not is_scene_configured(self, scene.render):
            return {"CANCELLED"}

        if not bpy.data.filepath:
            self.report({"ERROR"}, "Please save the scene")
            return {"CANCELLED"}

        bpy.ops.wm.save_mainfile()
        blender_exe = bpy.app.binary_path
        blend_path = bpy.data.filepath
        num_instances = scene.pseudo_rendering_farm_instances

        # Resolve effective frame range
        if scene.prf_use_custom_range:
            eff_start = scene.prf_frame_start
            eff_end = scene.prf_frame_end
            if eff_start > eff_end:
                self.report({"ERROR"}, "Custom Start frame must be <= End frame")
                return {"CANCELLED"}
        else:
            eff_start = scene.frame_start
            eff_end = scene.frame_end

        Globals.active_render_processes.clear()
        Globals.start_time = time.time()
        Globals.is_rendering_active = True
        Globals.frames_done = 0
        Globals.eta_seconds = 0

        # Snapshot existing valid frames so we don't count them as new
        snap = scan_output_folder(
            eff_start, eff_end, scene.render.filepath
        )
        Globals.snapshot_frames = snap["valid"]
        Globals.frames_total = snap["total_expected"]

        if not bpy.app.timers.is_registered(check_render_status):
            bpy.app.timers.register(check_render_status)

        for i in range(num_instances):
            factory = ["--factory-startup", "--disable-autoexec"] if scene.prf_load_user_addons else []
            cmd = [blender_exe] + factory + ["-b", blend_path, "-a"]
            try:
                if is_system_balanced():
                    subrange = get_worker_subrange(
                        eff_start, eff_end, num_instances, i
                    )
                    if not subrange:
                        continue
                    subrange_start, subrange_end = subrange
                    cmd = (
                        cmd[:-1]
                        + ["-s", str(subrange_start), "-e", str(subrange_end)]
                        + cmd[-1:]
                    )
                Globals.active_render_processes.append(
                    subprocess.Popen(
                        cmd,
                        env=get_env_for_instance(i),
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        **get_process_priority_kwargs(),
                    )
                )
            except Exception as e:
                self.report({"ERROR"}, f"Failed to launch instance {i}: {e}")

        self.report({"INFO"}, f"Launched {num_instances} render instances.")
        return {"FINISHED"}


class RENDER_OT_cancel_pseudo_rendering_farm(bpy.types.Operator):
    """Stop all background render processes spawned by this plugin"""

    bl_idname = "render.cancel_pseudo_rendering_farm"
    bl_label = "Cancel All Renders"

    def execute(self, context):
        if not Globals.active_render_processes:
            self.report({"INFO"}, "No active processes found")
            return {"FINISHED"}

        count = len([p for p in Globals.active_render_processes if p.poll() is None])
        if Globals.is_benchmarking:
            Globals.early_exit_benchmark = True
        Globals.is_benchmarking = False

        terminate_all_processes()
        time.sleep(0.2)
        cleared = cleanup_corrupted_frames()
        cleanup_bench_dir()

        for area in context.screen.areas:
            area.tag_redraw()

        if count != 0:
            self.report(
                {"WARNING"},
                f"Terminated {count} render processes. Removed {cleared} partial files",
            )
        return {"FINISHED"}


# --- Benchmarking ---


def launch_benchmark_iteration(context):
    """Spawns processes for the current benchmark step."""
    Globals.start_time = time.time()
    blender_exe = bpy.app.binary_path
    blend_path = bpy.data.filepath
    scene = bpy.context.scene
    frame_start = scene.frame_start
    frame_end = scene.frame_end
    available = frame_end - frame_start + 1
    Globals.benchmark_frames = min(48, available)

    Globals.bench_status_msg = f"Testing {Globals.current_bench_instances} instances on {Globals.benchmark_frames} frames"

    out_path = os.path.join(
        Globals.bench_temp_dir, f"inst_{Globals.current_bench_instances}", "frame_"
    )
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    for i in range(Globals.current_bench_instances):
        factory = ["--factory-startup", "--disable-autoexec"] if bpy.context.scene.prf_load_user_addons else []
        cmd = [blender_exe] + factory + ["-b", blend_path, "-o", out_path, "-a"]
        if is_system_balanced():
            subrange = get_worker_subrange(
                scene.frame_start,
                frame_start + Globals.benchmark_frames - 1,
                Globals.current_bench_instances,
                i,
            )
            if not subrange:
                continue
            subrange_start, subrange_end = subrange
            cmd = (
                cmd[:-1]
                + ["-s", str(subrange_start), "-e", str(subrange_end)]
                + cmd[-1:]
            )

        Globals.active_render_processes.append(
            subprocess.Popen(
                cmd,
                env=get_env_for_instance(i),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **get_process_priority_kwargs(),
            )
        )

    if not bpy.app.timers.is_registered(check_render_status):
        bpy.app.timers.register(check_render_status)


class RENDER_OT_benchmarking(bpy.types.Operator):
    """Launch pseudo rendering farm benchmarking"""

    bl_idname = "render.benchmarking"
    bl_label = "Launch benchmark"

    def execute(self, context):
        if not is_scene_configured(self, context.scene.render):
            return {"CANCELLED"}

        if not bpy.data.filepath:
            self.report({"ERROR"}, "Save file before benchmarking.")
            return {"CANCELLED"}

        bpy.ops.wm.save_mainfile()

        Globals.is_benchmarking = True
        Globals.current_bench_instances = 1
        Globals.benchmark_results = {}
        Globals.declining_streak = 0
        Globals.peak_throughput = 0
        Globals.bench_temp_dir = tempfile.mkdtemp(prefix="blender_bench_")

        launch_benchmark_iteration(context)
        return {"FINISHED"}


# --- Custom range auto-detect ---


class RENDER_OT_autodetect_start_frame(bpy.types.Operator):
    """Scan output folder and set Start to the first missing frame in range"""

    bl_idname = "render.prf_autodetect_start"
    bl_label = "Auto-detect from folder"

    def execute(self, context):
        scene = context.scene
        frame_start = scene.frame_start
        frame_end = scene.frame_end

        scan = scan_output_folder(frame_start, frame_end, scene.render.filepath)
        valid = scan["valid"]

        if not valid:
            # Nothing rendered yet — start from the beginning
            scene.prf_frame_start = frame_start
            scene.prf_frame_end = frame_end
            self.report({"INFO"}, f"No rendered frames found. Starting from {frame_start}")
            return {"FINISHED"}

        # Find the first frame in range that is missing
        first_missing = None
        for f in range(frame_start, frame_end + 1):
            if f not in valid:
                first_missing = f
                break

        if first_missing is None:
            self.report({"INFO"}, "All frames already rendered!")
            scene.prf_frame_start = frame_end
            scene.prf_frame_end = frame_end
        else:
            scene.prf_frame_start = first_missing
            scene.prf_frame_end = frame_end
            self.report(
                {"INFO"},
                f"Found {len(valid)} existing frames. Resuming from frame {first_missing}",
            )

        return {"FINISHED"}


# --- Output path selector ---


class RENDER_OT_set_project_output(bpy.types.Operator):
    """Set render output to a 'render' subfolder next to the saved .blend file"""

    bl_idname = "render.prf_set_project_output"
    bl_label = "Set to project folder"

    def execute(self, context):
        if not bpy.data.filepath:
            self.report({"ERROR"}, "Save the project first")
            return {"CANCELLED"}

        blend_dir = os.path.dirname(bpy.path.abspath(bpy.data.filepath))
        render_dir = os.path.join(blend_dir, "render", "")
        context.scene.render.filepath = render_dir
        os.makedirs(render_dir, exist_ok=True)
        self.report({"INFO"}, f"Output set to: {render_dir}")
        return {"FINISHED"}


# --- Output folder management ---


class RENDER_OT_clear_output_folder(bpy.types.Operator):
    """Delete all valid rendered frames in the current output folder for the active range"""

    bl_idname = "render.prf_clear_output"
    bl_label = "Clear Output Folder"

    _frames_found: int = 0

    def invoke(self, context, event):
        scene = context.scene
        eff_start = scene.prf_frame_start if scene.prf_use_custom_range else scene.frame_start
        eff_end = scene.prf_frame_end if scene.prf_use_custom_range else scene.frame_end

        scan = scan_output_folder(eff_start, eff_end, scene.render.filepath)
        self._frames_found = len(scan["valid"])

        if self._frames_found == 0:
            self.report({"INFO"}, "No rendered frames found in output folder")
            return {"CANCELLED"}

        return context.window_manager.invoke_props_dialog(self, width=420, title="Clear Output Folder")

    def draw(self, context):
        scene = context.scene
        eff_start = scene.prf_frame_start if scene.prf_use_custom_range else scene.frame_start
        eff_end = scene.prf_frame_end if scene.prf_use_custom_range else scene.frame_end

        layout = self.layout
        layout.label(
            text=f"{self._frames_found} frame(s) in range {eff_start}\u2013{eff_end} will be permanently deleted.",
            icon="ERROR",
        )
        layout.label(text="This cannot be undone. Press OK to confirm.")

    def execute(self, context):
        scene = context.scene
        eff_start = scene.prf_frame_start if scene.prf_use_custom_range else scene.frame_start
        eff_end = scene.prf_frame_end if scene.prf_use_custom_range else scene.frame_end

        scan = scan_output_folder(eff_start, eff_end, scene.render.filepath)
        output_dir = os.path.dirname(bpy.path.abspath(scene.render.filepath))
        output_prefix = scene.render.filepath
        deleted = 0

        for filename in os.listdir(output_dir):
            file_path = os.path.join(output_dir, filename)
            if not os.path.isfile(file_path):
                continue
            frame_num = get_frame_number_from_filename(filename, output_prefix)
            if frame_num is None:
                continue
            if eff_start <= frame_num <= eff_end:
                try:
                    os.remove(file_path)
                    deleted += 1
                except Exception as e:
                    print(f"PseudoRenderingFarmEX: Failed to delete {filename}: {e}")

        self.report({"INFO"}, f"Deleted {deleted} frame(s) from output folder")
        return {"FINISHED"}


class RENDER_OT_set_output_subfolder(bpy.types.Operator):
    """Append a version suffix to the output path (e.g. /frames/ -> /frames_v2/)"""

    bl_idname = "render.prf_set_output_subfolder"
    bl_label = "Apply Version Suffix"

    def execute(self, context):
        scene = context.scene
        suffix = scene.prf_output_suffix.strip()

        if not suffix:
            self.report({"ERROR"}, "Version suffix is empty")
            return {"CANCELLED"}

        current = bpy.path.abspath(scene.render.filepath)
        # Strip trailing slash to work with the path cleanly
        current_stripped = current.rstrip("/\\") 
        new_path = current_stripped + suffix + os.sep

        # Store original path for restore if not already saved
        if not scene.prf_original_output:
            scene.prf_original_output = scene.render.filepath

        scene.render.filepath = new_path
        os.makedirs(new_path, exist_ok=True)
        self.report({"INFO"}, f"Output path set to: {new_path}")
        return {"FINISHED"}


class RENDER_OT_restore_output_path(bpy.types.Operator):
    """Restore the original output path before the version suffix was applied"""

    bl_idname = "render.prf_restore_output"
    bl_label = "Restore Original Path"

    def execute(self, context):
        scene = context.scene
        if not scene.prf_original_output:
            self.report({"INFO"}, "No original path saved")
            return {"CANCELLED"}

        scene.render.filepath = scene.prf_original_output
        scene.prf_original_output = ""
        self.report({"INFO"}, f"Output path restored to: {scene.render.filepath}")
        return {"FINISHED"}


# --- Multi-GPU setup ---


class RENDER_OT_setup_multi_gpu(bpy.types.Operator):
    """Detect and setup multiple GPUs for parallel rendering"""

    bl_idname = "render.setup_multi_gpu"
    bl_label = "Setup multi-GPU"

    def execute(self, context):
        Globals.gpu_detection_active = True

        if not bpy.app.timers.is_registered(check_multi_gpu_status):
            bpy.app.timers.register(check_multi_gpu_status)

        for area in context.screen.areas:
            area.tag_redraw()
        return {"FINISHED"}


# --- UI ---


class RENDER_OT_open_folder(bpy.types.Operator):
    """Open the folder with rendered data"""

    bl_idname = "render.open_folder"
    bl_label = "Open folder"

    def sanitize(self, context):
        frame_path = Path(
            context.scene.render.frame_path(frame=context.scene.frame_current)
        )
        folder_path = frame_path.parent.absolute()
        return folder_path

    def execute(self, context):
        render_path = self.sanitize(context)

        if not os.path.exists(render_path):
            return {"CANCELLED"}

        current_os = platform.system()
        if current_os == "Windows":
            os.startfile(render_path)
        elif current_os == "Darwin":
            subprocess.run(["open", render_path])
        else:
            subprocess.run(["xdg-open", render_path])

        return {"FINISHED"}


class RENDER_PT_pseudo_rendering_farm_panel(bpy.types.Panel):
    bl_label = "Pseudo Rendering Farm EX"
    bl_idname = "RENDER_PT_pseudo_rendering_farm_ex"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "render"

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        is_running = (
            any(p.poll() is None for p in Globals.active_render_processes)
            or Globals.is_benchmarking
        )
        col = layout.column(align=True)

        sub_col = col.column()
        sub_col.enabled = not is_running
        sub_col.prop(scene, "pseudo_rendering_farm_instances", text="Instances")
        sub_col.prop(scene, "prf_load_user_addons")

        # Custom frame range
        range_col = col.column(align=True)
        range_col.enabled = not is_running
        range_col.prop(scene, "prf_use_custom_range")
        if scene.prf_use_custom_range:
            range_row = range_col.row(align=True)
            range_row.prop(scene, "prf_frame_start", text="Start")
            range_row.prop(scene, "prf_frame_end", text="End")
            range_col.operator(
                "render.prf_autodetect_start",
                icon="VIEWZOOM",
                text="Auto-detect from folder",
            )

        # Output folder management
        col.separator()
        folder_col = col.column(align=True)
        folder_col.enabled = not is_running
        folder_col.label(text="Output Folder:", icon="FILE_FOLDER")

        # Output path field + project folder button
        path_row = folder_col.row(align=True)
        path_row.prop(scene.render, "filepath", text="")
        path_row.operator(
            "render.prf_set_project_output",
            text="",
            icon="FILE_BLEND",
        )

        # Version suffix row
        suffix_row = folder_col.row(align=True)
        suffix_row.prop(scene, "prf_output_suffix", text="Suffix")
        suffix_row.operator("render.prf_set_output_subfolder", text="Apply", icon="ADD")

        # Restore button — only shown if a suffix is currently active
        if scene.prf_original_output:
            restore_row = folder_col.row(align=True)
            restore_row.label(
                text=f"Active: ...{os.path.basename(scene.render.filepath.rstrip(os.sep))}",
                icon="CHECKMARK",
            )
            restore_row.operator("render.prf_restore_output", text="Restore", icon="LOOP_BACK")

        # Clear frames button
        folder_col.operator(
            "render.prf_clear_output",
            text="Clear Output Folder",
            icon="TRASH",
        )

        col.separator()

        row = col.row(align=True)
        launch_row = row.row(align=True)
        launch_row.enabled = not is_running
        launch_row.operator("render.pseudo_rendering_farm", icon="RENDER_ANIMATION")
        benchmark_row = row.row(align=True)
        benchmark_row.enabled = not is_running
        benchmark_row.operator("render.benchmarking", icon="SETTINGS")

        row = col.row(align=True)
        open_row = row.row(align=True)
        open_row.operator(
            "render.open_folder", icon="FILE_FOLDER", text="Open render folder"
        )

        if len(Globals.gpu_devices) > 1:
            if not Globals.gpu_configured:
                gpu_row = col.row(align=True)
                gpu_row.enabled = not is_running and not Globals.gpu_detection_active
                gpu_row.operator("render.setup_multi_gpu", icon="LIGHT")

        row = col.row(align=True)
        cancel_row = row.row(align=True)
        cancel_row.enabled = is_running
        cancel_row.operator(
            "render.cancel_pseudo_rendering_farm", icon="X", text="Stop"
        )

        if len(Globals.gpu_devices) > 1:
            if Globals.gpu_configured:
                layout.label(
                    text=f"Multi-GPU: {len(Globals.gpu_devices)} devices",
                    icon="PREFERENCES",
                )

        if Globals.is_benchmarking:
            layout.label(text=Globals.bench_status_msg, icon="PLAY")
        elif Globals.is_rendering_active:
            total = Globals.frames_total
            done = Globals.frames_done
            active = len([p for p in Globals.active_render_processes if p.poll() is None])

            if total > 0:
                pct = done / total * 100
                layout.label(
                    text=f"Frames: {done}/{total}  ({pct:.0f}%)",
                    icon="RENDER_ANIMATION",
                )
            else:
                layout.label(
                    text=f"Rendering: {active} instance(s) active",
                    icon="RENDER_ANIMATION",
                )

            elapsed = time.time() - Globals.start_time
            layout.label(
                text=f"Elapsed: {format_time(elapsed)}",
                icon="TIME",
            )

            if Globals.eta_seconds > 0:
                layout.label(
                    text=f"Remaining: {format_time(Globals.eta_seconds)}",
                    icon="SORTTIME",
                )
        else:
            if Globals.elapsed_time != 0:
                layout.label(
                    text=f"Ready. Spent {Globals.elapsed_time:.1f} seconds with {Globals.seconds_per_frame:.1f} seconds per frame",
                    icon="CHECKMARK",
                )
            else:
                layout.label(text="Ready", icon="CHECKMARK")


classes = [
    RENDER_OT_pseudo_rendering_farm,
    RENDER_OT_cancel_pseudo_rendering_farm,
    RENDER_OT_benchmarking,
    RENDER_OT_autodetect_start_frame,
    RENDER_OT_set_project_output,
    RENDER_OT_clear_output_folder,
    RENDER_OT_set_output_subfolder,
    RENDER_OT_restore_output_path,
    RENDER_OT_setup_multi_gpu,
    RENDER_OT_open_folder,
    RENDER_PT_pseudo_rendering_farm_panel,
]


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.pseudo_rendering_farm_instances = bpy.props.IntProperty(
        name="Instances", default=2, min=1, max=32
    )
    bpy.types.Scene.prf_use_custom_range = bpy.props.BoolProperty(
        name="Custom Frame Range",
        description="Override scene frame range for this render",
        default=False,
    )
    bpy.types.Scene.prf_frame_start = bpy.props.IntProperty(
        name="Start",
        description="Custom start frame",
        default=1, min=0,
    )
    bpy.types.Scene.prf_frame_end = bpy.props.IntProperty(
        name="End",
        description="Custom end frame",
        default=250, min=0,
    )
    bpy.types.Scene.prf_output_suffix = bpy.props.StringProperty(
        name="Version Suffix",
        description="Suffix to append to output path (e.g. _v2, _test)",
        default="_v2",
    )
    bpy.types.Scene.prf_original_output = bpy.props.StringProperty(
        name="Original Output Path",
        description="Saved original output path before suffix was applied",
        default="",
    )
    bpy.types.Scene.prf_load_user_addons = bpy.props.BoolProperty(
        name="Load user add-ons in background",
        description="Pass --factory-startup to background instances (faster, lower memory). Disable only if your scene requires a specific add-on during render (e.g. a custom exporter or geometry node add-on)",
        default=True,
    )
    detect_gpus()


def unregister():
    cleanup_gpu_config()
    for c in classes:
        bpy.utils.unregister_class(c)
    del bpy.types.Scene.pseudo_rendering_farm_instances
    del bpy.types.Scene.prf_use_custom_range
    del bpy.types.Scene.prf_frame_start
    del bpy.types.Scene.prf_frame_end
    del bpy.types.Scene.prf_output_suffix
    del bpy.types.Scene.prf_original_output
    del bpy.types.Scene.prf_load_user_addons


if __name__ == "__main__":
    register()
