import gc
import json
import os
import queue
import threading
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

# GUI 里用 TkAgg 更稳，便于弹出 ROI 选择窗口
import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
from matplotlib.widgets import RectangleSelector

import hyperspy.api as hs
import mrcfile

try:
    from scipy.ndimage import gaussian_filter, shift as ndi_shift
    from scipy.io import loadmat
    HAS_SCIPY = True
except Exception:
    gaussian_filter = None
    ndi_shift = None
    loadmat = None
    HAS_SCIPY = False


APP_TITLE = "DM4 4D-STEM 对齐与导出工具"
APP_VERSION = "1.0.0"


@dataclass
class ProcessingParams:
    input_path: str
    output_dir: str
    output_basename: str
    output_format: str  # 'mrc' or 'img'
    bin_nav: int
    bin_sig: int
    out_row_chunk: int
    enable_alignment: bool
    alignment_mode: str  # 'roi' or 'mat'
    alignment_mat_path: str
    alignment_mat_var: str
    alignment_mat_columns: str  # 'dx_dy' or 'dy_dx'
    reference_nav_y: int | None
    reference_nav_x: int | None
    smooth_sigma: float
    refine_radius: int
    bg_percentile: float
    subpixel: bool
    roi: dict | None
    save_shift_plot: bool
    save_shift_csv: bool
    save_format_info: bool
    lazy_load: bool


class Logger:
    def __init__(self, callback):
        self.callback = callback

    def log(self, text: str):
        self.callback(text)


class StackWriter:
    def __init__(self, output_path: str, fmt: str, shape: tuple[int, int, int], logger: Logger):
        self.output_path = output_path
        self.fmt = fmt.lower()
        self.shape = shape
        self.logger = logger
        self._mrc = None
        self._img = None
        self.data = None

        if self.fmt == "mrc":
            self._mrc = mrcfile.new_mmap(output_path, shape=shape, mrc_mode=2, overwrite=True)
            self.data = self._mrc.data
        elif self.fmt == "img":
            # 这里的 img 采用 headerless float32 原始堆栈
            # 真实 shape / dtype / frame 顺序记录在 companion json/txt 中
            self._img = np.memmap(output_path, dtype=np.float32, mode="w+", shape=shape)
            self.data = self._img
        else:
            raise ValueError(f"Unsupported format: {fmt}")

    def write(self, start_idx: int, block: np.ndarray):
        end_idx = start_idx + block.shape[0]
        self.data[start_idx:end_idx] = block

    def flush(self):
        if self._mrc is not None:
            self._mrc.flush()
        if self._img is not None:
            self._img.flush()

    def close(self):
        try:
            self.flush()
        finally:
            if self._mrc is not None:
                self._mrc.close()
                self._mrc = None
            self._img = None
            self.data = None


def find_4d_signal(signals):
    items = signals if isinstance(signals, list) else [signals]
    for sig in items:
        data = getattr(sig, "data", None)
        if getattr(data, "ndim", None) == 4:
            return sig
    raise ValueError("No 4D STEM signal found in this file.")


def load_4d_stem_data(input_path, lazy=True):
    try:
        signals = hs.load(input_path, lazy=lazy)
    except TypeError:
        signals = hs.load(input_path)
    sig4 = find_4d_signal(signals)
    return signals, sig4, sig4.data


def clamp_roi(roi, frame_shape):
    h, w = frame_shape
    x_min = max(0, min(int(roi["x_min"]), w - 1))
    x_max = max(x_min + 1, min(int(roi["x_max"]), w))
    y_min = max(0, min(int(roi["y_min"]), h - 1))
    y_max = max(y_min + 1, min(int(roi["y_max"]), h))
    return {"x_min": x_min, "x_max": x_max, "y_min": y_min, "y_max": y_max}


def percentile_window(frame, vmin_percentile=1, vmax_percentile=99):
    frame = np.asarray(frame, dtype=np.float32)
    vmin, vmax = np.percentile(frame, [vmin_percentile, vmax_percentile])
    if not np.isfinite(vmin):
        vmin = float(np.nanmin(frame))
    if not np.isfinite(vmax):
        vmax = float(np.nanmax(frame))
    if vmin == vmax:
        vmax = vmin + 1e-6
    return vmin, vmax


def get_reference_frame(input_path, nav_y=None, nav_x=None, lazy=True, out_dtype=np.float32):
    signals, sig4, data = load_4d_stem_data(input_path, lazy=lazy)
    sy, sx, _, _ = data.shape
    if nav_y is None:
        nav_y = sy // 2
    if nav_x is None:
        nav_x = sx // 2
    if not (0 <= nav_y < sy and 0 <= nav_x < sx):
        raise IndexError(f"Navigation index out of range: ({nav_y}, {nav_x}) not in {(sy, sx)}")
    frame = np.asarray(data[nav_y, nav_x], dtype=out_dtype)
    del signals, sig4, data
    gc.collect()
    return frame, (nav_y, nav_x)


def select_search_roi_interactive(frame, title="拖拽框选 ROI，完成后按 Enter，取消按 Esc"):
    frame = np.asarray(frame, dtype=np.float32)
    vmin, vmax = percentile_window(frame)

    fig, ax = plt.subplots(figsize=(7, 7))
    ax.imshow(frame, cmap="gray", origin="upper", vmin=vmin, vmax=vmax)
    ax.set_title(title)

    result = {"roi": None}

    selector = RectangleSelector(
        ax,
        lambda eclick, erelease: None,
        useblit=True,
        button=[1],
        minspanx=2,
        minspany=2,
        interactive=True,
        drag_from_anywhere=True,
    )

    def on_key(event):
        if event.key in ("enter", "return"):
            x1, x2, y1, y2 = selector.extents
            x_min = int(np.floor(min(x1, x2)))
            x_max = int(np.ceil(max(x1, x2)))
            y_min = int(np.floor(min(y1, y2)))
            y_max = int(np.ceil(max(y1, y2)))
            roi = clamp_roi(
                {"x_min": x_min, "x_max": x_max, "y_min": y_min, "y_max": y_max},
                frame.shape,
            )
            result["roi"] = roi
            plt.close(fig)
        elif event.key == "escape":
            result["roi"] = None
            plt.close(fig)

    fig.canvas.mpl_connect("key_press_event", on_key)
    plt.show()
    return result["roi"]


def find_center_spot_in_roi(frame, search_roi, smooth_sigma=1.0, refine_radius=8, bg_percentile=20):
    frame = np.asarray(frame, dtype=np.float32)
    roi = clamp_roi(search_roi, frame.shape)

    x_min, x_max = roi["x_min"], roi["x_max"]
    y_min, y_max = roi["y_min"], roi["y_max"]
    patch = frame[y_min:y_max, x_min:x_max]

    if patch.size == 0:
        raise ValueError("ROI patch is empty.")

    work = patch
    if HAS_SCIPY and smooth_sigma is not None and smooth_sigma > 0:
        work = gaussian_filter(patch, smooth_sigma)

    peak_rel_y, peak_rel_x = np.unravel_index(np.argmax(work), work.shape)

    ly0 = max(0, peak_rel_y - refine_radius)
    ly1 = min(patch.shape[0], peak_rel_y + refine_radius + 1)
    lx0 = max(0, peak_rel_x - refine_radius)
    lx1 = min(patch.shape[1], peak_rel_x + refine_radius + 1)

    local = patch[ly0:ly1, lx0:lx1].astype(np.float32)
    bg = np.percentile(local, bg_percentile)
    weights = np.clip(local - bg, 0, None)

    if np.sum(weights) <= 0:
        cy = float(y_min + peak_rel_y)
        cx = float(x_min + peak_rel_x)
    else:
        yy, xx = np.indices(local.shape, dtype=np.float32)
        cy_local = float((weights * yy).sum() / weights.sum())
        cx_local = float((weights * xx).sum() / weights.sum())
        cy = float(y_min + ly0 + cy_local)
        cx = float(x_min + lx0 + cx_local)

    return {
        "cy": cy,
        "cx": cx,
        "peak_y": int(y_min + peak_rel_y),
        "peak_x": int(x_min + peak_rel_x),
        "roi": roi,
    }


def shift_frame_no_wrap(frame, dy, dx, cval=0.0, subpixel=True):
    frame = np.asarray(frame, dtype=np.float32)

    if subpixel and HAS_SCIPY:
        return ndi_shift(
            frame,
            shift=(dy, dx),
            order=1,
            mode="constant",
            cval=cval,
            prefilter=False,
        ).astype(np.float32)

    dy_i = int(np.rint(dy))
    dx_i = int(np.rint(dx))

    out = np.full_like(frame, cval, dtype=np.float32)
    h, w = frame.shape

    src_y0 = max(0, -dy_i)
    src_y1 = min(h, h - dy_i)
    src_x0 = max(0, -dx_i)
    src_x1 = min(w, w - dx_i)

    if src_y1 <= src_y0 or src_x1 <= src_x0:
        return out

    dst_y0 = max(0, dy_i)
    dst_y1 = dst_y0 + (src_y1 - src_y0)
    dst_x0 = max(0, dx_i)
    dst_x1 = dst_x0 + (src_x1 - src_x0)

    out[dst_y0:dst_y1, dst_x0:dst_x1] = frame[src_y0:src_y1, src_x0:src_x1]
    return out


def save_shift_csv(csv_path, centers, shifts):
    sy, sx, _ = centers.shape
    rows = []
    for ny in range(sy):
        for nx in range(sx):
            cy, cx = centers[ny, nx]
            dy, dx = shifts[ny, nx]
            rows.append([ny, nx, cy, cx, dy, dx])
    rows = np.asarray(rows, dtype=np.float32)
    header = "nav_y,nav_x,center_y,center_x,shift_y,shift_x"
    np.savetxt(csv_path, rows, delimiter=",", header=header, comments="", fmt="%.6f")


def save_shift_curve_csv(csv_path, shifts):
    shifts = np.asarray(shifts, dtype=np.float32)
    shift_y = shifts[..., 0].reshape(-1)
    shift_x = shifts[..., 1].reshape(-1)
    frame_idx = np.arange(shift_y.size)
    data = np.column_stack([frame_idx, shift_y, shift_x])
    np.savetxt(
        csv_path,
        data,
        delimiter=",",
        header="frame_index,shift_y,shift_x",
        comments="",
        fmt=["%d", "%.6f", "%.6f"],
    )


def save_shift_only_csv(csv_path, shifts):
    shifts = np.asarray(shifts, dtype=np.float32)
    sy, sx, _ = shifts.shape
    rows = []
    for ny in range(sy):
        for nx in range(sx):
            dy, dx = shifts[ny, nx]
            rows.append([ny, nx, dy, dx])
    rows = np.asarray(rows, dtype=np.float32)
    np.savetxt(
        csv_path,
        rows,
        delimiter=",",
        header="nav_y,nav_x,shift_y,shift_x",
        comments="",
        fmt="%.6f",
    )


def load_alignment_from_mat(mat_path, sy, sx, trim_y, variable_name="alignment", column_order="dx_dy"):
    if loadmat is None:
        raise RuntimeError("scipy.io.loadmat is unavailable. Please install scipy.")

    if not os.path.exists(mat_path):
        raise FileNotFoundError(f"alignment.mat not found: {mat_path}")

    mat = loadmat(mat_path)
    arr = None
    used_name = None

    if variable_name and variable_name in mat:
        arr = mat[variable_name]
        used_name = variable_name
    else:
        for k, v in mat.items():
            if k.startswith("__"):
                continue
            if isinstance(v, np.ndarray) and np.issubdtype(v.dtype, np.number):
                if v.ndim in (2, 3):
                    arr = v
                    used_name = k
                    break

    if arr is None:
        raise ValueError("No suitable numeric variable found in alignment.mat")

    arr = np.asarray(arr, dtype=np.float32).squeeze()

    if arr.ndim == 3 and arr.shape[-1] >= 2:
        if arr.shape[0] not in (sy, trim_y) or arr.shape[1] != sx:
            raise ValueError(f"Unsupported MAT alignment shape: {arr.shape}; expected ({sy}, {sx}, 2) or ({trim_y}, {sx}, 2)")
        shifts_xy = arr[:trim_y, :sx, :2]
    elif arr.ndim == 2:
        if arr.shape[1] >= 2 and arr.shape[0] in (sy * sx, trim_y * sx):
            shifts_xy = arr[:trim_y * sx, :2].reshape(trim_y, sx, 2)
        elif arr.shape[0] >= 2 and arr.shape[1] in (sy * sx, trim_y * sx):
            shifts_xy = arr[:2, :trim_y * sx].T.reshape(trim_y, sx, 2)
        else:
            raise ValueError(
                f"Unsupported MAT alignment shape: {arr.shape}; expected (N,2), (2,N), or (sy,sx,2) with N=sy*sx"
            )
    else:
        raise ValueError(f"Unsupported MAT alignment ndim: {arr.ndim}")

    if column_order == "dx_dy":
        dx = shifts_xy[..., 0]
        dy = shifts_xy[..., 1]
    else:
        dy = shifts_xy[..., 0]
        dx = shifts_xy[..., 1]

    shifts_yx = np.stack([dy, dx], axis=-1).astype(np.float32)
    return used_name, shifts_yx


def plot_shifts_over_frames(shifts, save_path):
    shifts = np.asarray(shifts, dtype=np.float32)
    shift_y = shifts[..., 0].reshape(-1)
    shift_x = shifts[..., 1].reshape(-1)
    frame_idx = np.arange(shift_y.size)

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(frame_idx, shift_y, label="shift_y")
    ax.plot(frame_idx, shift_x, label="shift_x")
    ax.set_xlabel("Frame index")
    ax.set_ylabel("Shift (pixels)")
    ax.set_title("Frame-by-frame center-spot shifts")
    ax.grid(True, alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(save_path, dpi=200, bbox_inches="tight")
    plt.close(fig)


def make_output_base(params: ProcessingParams) -> str:
    output_dir = Path(params.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if params.output_basename.strip():
        stem = params.output_basename.strip()
    else:
        stem = Path(params.input_path).stem
        if params.enable_alignment:
            stem += "_aligned"
        stem += f"_navbin{params.bin_nav}_sigbin{params.bin_sig}"
    return str(output_dir / stem)


def write_format_info(base_path: str, info: dict):
    json_path = base_path + "_format_info.json"
    txt_path = base_path + "_format_info.txt"

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)

    lines = []
    for k, v in info.items():
        lines.append(f"{k}: {v}")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return json_path, txt_path


def process_dm4_to_stack(params: ProcessingParams, logger: Logger, progress_cb=None):
    logger.log(f"Loading: {params.input_path}")
    signals, sig4, data = load_4d_stem_data(params.input_path, lazy=params.lazy_load)
    sy, sx, kx, ky = data.shape
    logger.log(f"Input shape: {(sy, sx, kx, ky)}")

    if params.reference_nav_y is None:
        ref_nav_y = sy // 2
    else:
        ref_nav_y = params.reference_nav_y
    if params.reference_nav_x is None:
        ref_nav_x = sx // 2
    else:
        ref_nav_x = params.reference_nav_x

    if not (0 <= ref_nav_y < sy and 0 <= ref_nav_x < sx):
        raise ValueError(f"Reference navigation out of range: {(ref_nav_y, ref_nav_x)} not in {(sy, sx)}")

    out_sy = sy // params.bin_nav
    out_sx = sx // params.bin_nav
    out_kx = kx // params.bin_sig
    out_ky = ky // params.bin_sig
    if min(out_sy, out_sx, out_kx, out_ky) <= 0:
        raise ValueError("Binning factor is too large for the input shape.")

    trim_y = out_sy * params.bin_nav
    trim_x = out_sx * params.bin_nav
    trim_kx = out_kx * params.bin_sig
    trim_ky = out_ky * params.bin_sig

    if trim_y != sy or trim_x != sx or trim_kx != kx or trim_ky != ky:
        logger.log(
            f"Warning: input will be trimmed from {(sy, sx, kx, ky)} to {(trim_y, trim_x, trim_kx, trim_ky)} "
            f"for nav_bin={params.bin_nav}, sig_bin={params.bin_sig}."
        )

    reference_center = None
    imported_shifts = None
    used_mat_var = None

    if params.enable_alignment:
        if params.alignment_mode == "mat":
            used_mat_var, imported_shifts = load_alignment_from_mat(
                params.alignment_mat_path,
                sy=sy,
                sx=sx,
                trim_y=trim_y,
                variable_name=params.alignment_mat_var.strip() or "alignment",
                column_order=params.alignment_mat_columns,
            )
            logger.log(
                f"Loaded alignment.mat: {params.alignment_mat_path} | variable={used_mat_var} | shape={imported_shifts.shape} | columns={params.alignment_mat_columns}"
            )
        else:
            if params.roi is None:
                raise ValueError("Alignment mode=ROI, but no ROI is selected.")
            reference_frame = np.asarray(data[ref_nav_y, ref_nav_x], dtype=np.float32)
            ref_info = find_center_spot_in_roi(
                reference_frame,
                search_roi=params.roi,
                smooth_sigma=params.smooth_sigma,
                refine_radius=params.refine_radius,
                bg_percentile=params.bg_percentile,
            )
            reference_center = (ref_info["cy"], ref_info["cx"])
            logger.log(
                f"Reference nav: {(ref_nav_y, ref_nav_x)}, reference center: "
                f"(y={reference_center[0]:.3f}, x={reference_center[1]:.3f})"
            )
    else:
        logger.log("Alignment disabled.")

    output_base = make_output_base(params)
    output_ext = ".mrc" if params.output_format.lower() == "mrc" else ".img"
    output_path = output_base + output_ext

    n_frames = out_sy * out_sx
    logger.log(f"Output stack shape: {(n_frames, out_kx, out_ky)}")
    writer = StackWriter(output_path, params.output_format, (n_frames, out_kx, out_ky), logger)

    all_centers = None
    if params.enable_alignment and params.alignment_mode == "roi":
        all_centers = np.full((trim_y, sx, 2), np.nan, dtype=np.float32)
    all_shifts = np.zeros((trim_y, sx, 2), dtype=np.float32) if params.enable_alignment else None

    out_row_chunk = max(1, int(params.out_row_chunk))
    frame_offset = 0

    try:
        for oy0 in range(0, out_sy, out_row_chunk):
            oy1 = min(oy0 + out_row_chunk, out_sy)
            y0 = oy0 * params.bin_nav
            y1 = oy1 * params.bin_nav

            block = np.asarray(data[y0:y1, :sx, :kx, :ky], dtype=np.float32)

            if params.enable_alignment:
                if params.alignment_mode == "mat":
                    shifts_block = imported_shifts[y0:y1]
                    for iy in range(y1 - y0):
                        for ix in range(sx):
                            dy, dx = shifts_block[iy, ix]
                            block[iy, ix] = shift_frame_no_wrap(
                                block[iy, ix],
                                dy=float(dy),
                                dx=float(dx),
                                cval=0.0,
                                subpixel=params.subpixel,
                            )
                    all_shifts[y0:y1] = shifts_block
                else:
                    centers_block = np.empty((y1 - y0, sx, 2), dtype=np.float32)
                    shifts_block = np.empty((y1 - y0, sx, 2), dtype=np.float32)

                    for iy in range(y1 - y0):
                        for ix in range(sx):
                            frame = block[iy, ix]
                            center = find_center_spot_in_roi(
                                frame,
                                search_roi=params.roi,
                                smooth_sigma=params.smooth_sigma,
                                refine_radius=params.refine_radius,
                                bg_percentile=params.bg_percentile,
                            )
                            cy, cx = center["cy"], center["cx"]
                            dy = reference_center[0] - cy
                            dx = reference_center[1] - cx
                            block[iy, ix] = shift_frame_no_wrap(
                                frame,
                                dy=dy,
                                dx=dx,
                                cval=0.0,
                                subpixel=params.subpixel,
                            )
                            centers_block[iy, ix] = (cy, cx)
                            shifts_block[iy, ix] = (dy, dx)

                    all_centers[y0:y1] = centers_block
                    all_shifts[y0:y1] = shifts_block

            block = block[:, :trim_x, :trim_kx, :trim_ky]
            n_out_rows_chunk = oy1 - oy0
            block = block.reshape(
                n_out_rows_chunk,
                params.bin_nav,
                out_sx,
                params.bin_nav,
                out_kx,
                params.bin_sig,
                out_ky,
                params.bin_sig,
            ).sum(axis=(1, 3, 5, 7), dtype=np.float32)

            flat_block = block.reshape(n_out_rows_chunk * out_sx, out_kx, out_ky)
            writer.write(frame_offset, flat_block)
            frame_offset += flat_block.shape[0]

            logger.log(f"Processed output rows: {oy0} -> {oy1 - 1}")
            if progress_cb is not None:
                progress_cb(oy1 / out_sy)

            del block, flat_block
            gc.collect()

        writer.flush()
    finally:
        writer.close()
        del signals, sig4, data
        gc.collect()

    logger.log(f"Saved stack: {os.path.abspath(output_path)}")

    info = {
        "app_title": APP_TITLE,
        "app_version": APP_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "input_path": os.path.abspath(params.input_path),
        "output_path": os.path.abspath(output_path),
        "output_format": params.output_format.lower(),
        "output_dtype": "float32",
        "input_shape": [int(sy), int(sx), int(kx), int(ky)],
        "trimmed_input_shape": [int(trim_y), int(trim_x), int(trim_kx), int(trim_ky)],
        "output_stack_shape": [int(n_frames), int(out_kx), int(out_ky)],
        "frame_order": "nav_y-major then nav_x-major after alignment and navigation binning",
        "navigation_binning": int(params.bin_nav),
        "diffraction_binning": int(params.bin_sig),
        "alignment_enabled": bool(params.enable_alignment),
        "alignment_mode": params.alignment_mode if params.enable_alignment else "disabled",
        "alignment_mat_path": params.alignment_mat_path if params.enable_alignment and params.alignment_mode == "mat" else None,
        "alignment_mat_variable": used_mat_var if params.enable_alignment and params.alignment_mode == "mat" else None,
        "alignment_mat_columns": params.alignment_mat_columns if params.enable_alignment and params.alignment_mode == "mat" else None,
        "reference_nav": [int(ref_nav_y), int(ref_nav_x)],
        "reference_center": None if reference_center is None else [float(reference_center[0]), float(reference_center[1])],
        "search_roi": params.roi if params.enable_alignment and params.alignment_mode == "roi" else None,
        "smooth_sigma": float(params.smooth_sigma),
        "refine_radius": int(params.refine_radius),
        "bg_percentile": float(params.bg_percentile),
        "subpixel_alignment": bool(params.subpixel),
        "lazy_load": bool(params.lazy_load),
        "img_format_note": (
            "When output_format is img, the file is a headerless raw float32 stack. "
            "Use this format_info file to recover the exact stack shape and frame order."
            if params.output_format.lower() == "img" else None
        ),
    }

    generated_files = [output_path]
    if params.save_format_info:
        json_path, txt_path = write_format_info(output_base, info)
        generated_files.extend([json_path, txt_path])
        logger.log(f"Saved format info: {json_path}")
        logger.log(f"Saved format info: {txt_path}")

    if params.enable_alignment and all_shifts is not None:
        shifts_npy_path = output_base + "_shifts.npy"
        np.save(shifts_npy_path, all_shifts)
        generated_files.append(shifts_npy_path)
        logger.log(f"Saved shifts: {shifts_npy_path}")

        if all_centers is not None:
            centers_npy_path = output_base + "_centers.npy"
            np.save(centers_npy_path, all_centers)
            generated_files.append(centers_npy_path)
            logger.log(f"Saved centers: {centers_npy_path}")

        if params.save_shift_csv:
            if all_centers is not None:
                shift_csv_path = output_base + "_shifts.csv"
                save_shift_csv(shift_csv_path, all_centers, all_shifts)
                generated_files.append(shift_csv_path)
                logger.log(f"Saved shift table: {shift_csv_path}")
            else:
                shift_csv_path = output_base + "_shifts.csv"
                save_shift_only_csv(shift_csv_path, all_shifts)
                generated_files.append(shift_csv_path)
                logger.log(f"Saved shift table: {shift_csv_path}")

            shift_curve_csv_path = output_base + "_shift_curve.csv"
            save_shift_curve_csv(shift_curve_csv_path, all_shifts)
            generated_files.append(shift_curve_csv_path)
            logger.log(f"Saved shift curve: {shift_curve_csv_path}")

        if params.save_shift_plot:
            shift_plot_path = output_base + "_shift_plot.png"
            plot_shifts_over_frames(all_shifts, shift_plot_path)
            generated_files.append(shift_plot_path)
            logger.log(f"Saved shift plot: {shift_plot_path}")

    info["generated_files"] = [os.path.abspath(p) for p in generated_files]
    if params.save_format_info:
        write_format_info(output_base, info)

    return info


class DM4ToolGUI:
    def __init__(self, root):
        self.root = root
        self.root.title(f"{APP_TITLE} v{APP_VERSION}")
        self.root.geometry("1180x960")

        self.log_queue = queue.Queue()
        self.worker_thread = None
        self.selected_roi = None
        self.loaded_shape = None

        self.var_input = tk.StringVar()
        self.var_output_dir = tk.StringVar(value=str(Path.cwd()))
        self.var_output_basename = tk.StringVar()
        self.var_output_format = tk.StringVar(value="mrc")
        self.var_bin_nav = tk.StringVar(value="1")
        self.var_bin_sig = tk.StringVar(value="1")
        self.var_out_row_chunk = tk.StringVar(value="1")
        self.var_enable_alignment = tk.BooleanVar(value=True)
        self.var_alignment_mode = tk.StringVar(value="roi")
        self.var_alignment_mat_path = tk.StringVar()
        self.var_alignment_mat_var = tk.StringVar(value="alignment")
        self.var_alignment_mat_columns = tk.StringVar(value="dx_dy")
        self.var_ref_nav_y = tk.StringVar()
        self.var_ref_nav_x = tk.StringVar()
        self.var_smooth_sigma = tk.StringVar(value="1.0")
        self.var_refine_radius = tk.StringVar(value="8")
        self.var_bg_percentile = tk.StringVar(value="20")
        self.var_subpixel = tk.BooleanVar(value=True)
        self.var_save_shift_plot = tk.BooleanVar(value=True)
        self.var_save_shift_csv = tk.BooleanVar(value=True)
        self.var_save_format_info = tk.BooleanVar(value=True)
        self.var_lazy_load = tk.BooleanVar(value=True)
        self.var_shape_info = tk.StringVar(value="未加载")
        self.var_roi_info = tk.StringVar(value="未选择 ROI")
        self.var_progress_text = tk.StringVar(value="Idle")

        self._build_ui()
        self._poll_log_queue()

    def _build_ui(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        top = ttk.Frame(self.root, padding=10)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(1, weight=1)
        top.columnconfigure(3, weight=1)

        ttk.Label(top, text="输入 DM4 文件:").grid(row=0, column=0, sticky="w", padx=(0, 6), pady=4)
        ttk.Entry(top, textvariable=self.var_input).grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Button(top, text="选择文件", command=self.choose_input).grid(row=0, column=2, padx=6, pady=4)
        ttk.Button(top, text="读取 shape", command=self.load_shape).grid(row=0, column=3, sticky="w", pady=4)

        ttk.Label(top, text="输出目录:").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=4)
        ttk.Entry(top, textvariable=self.var_output_dir).grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Button(top, text="选择目录", command=self.choose_output_dir).grid(row=1, column=2, padx=6, pady=4)
        ttk.Label(top, text="输出文件名(可空):").grid(row=1, column=3, sticky="w", padx=(12, 6), pady=4)
        ttk.Entry(top, textvariable=self.var_output_basename).grid(row=1, column=4, sticky="ew", pady=4)
        top.columnconfigure(4, weight=1)

        notebook = ttk.Notebook(self.root)
        notebook.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))

        tab_settings = ttk.Frame(notebook, padding=10)
        tab_log = ttk.Frame(notebook, padding=10)
        notebook.add(tab_settings, text="参数与执行")
        notebook.add(tab_log, text="日志")

        self._build_settings_tab(tab_settings)
        self._build_log_tab(tab_log)

    def _build_settings_tab(self, parent):
        parent.columnconfigure(1, weight=1)
        parent.columnconfigure(3, weight=1)

        row = 0
        ttk.Label(parent, text="输出格式:").grid(row=row, column=0, sticky="w", pady=4)
        ttk.Combobox(parent, textvariable=self.var_output_format, values=["mrc", "img"], state="readonly", width=10).grid(row=row, column=1, sticky="w", pady=4)
        ttk.Label(parent, text="当前数据 shape:").grid(row=row, column=2, sticky="w", pady=4)
        ttk.Label(parent, textvariable=self.var_shape_info).grid(row=row, column=3, sticky="w", pady=4)

        row += 1
        ttk.Label(parent, text="Navigation bin:").grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=self.var_bin_nav, width=12).grid(row=row, column=1, sticky="w", pady=4)
        ttk.Label(parent, text="Diffraction bin:").grid(row=row, column=2, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=self.var_bin_sig, width=12).grid(row=row, column=3, sticky="w", pady=4)

        row += 1
        ttk.Label(parent, text="输出行块(out row chunk):").grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=self.var_out_row_chunk, width=12).grid(row=row, column=1, sticky="w", pady=4)
        ttk.Checkbutton(parent, text="Lazy load", variable=self.var_lazy_load).grid(row=row, column=2, sticky="w", pady=4)

        row += 1
        ttk.Checkbutton(parent, text="启用 alignment", variable=self.var_enable_alignment).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Checkbutton(parent, text="启用亚像素对齐(需 scipy)", variable=self.var_subpixel).grid(row=row, column=1, sticky="w", pady=4)
        ttk.Label(parent, text=f"SciPy 可用: {'是' if HAS_SCIPY else '否'}").grid(row=row, column=2, sticky="w", pady=4)

        row += 1
        ttk.Label(parent, text="Alignment 来源:").grid(row=row, column=0, sticky="w", pady=4)
        mode_frame = ttk.Frame(parent)
        mode_frame.grid(row=row, column=1, columnspan=3, sticky="w", pady=4)
        ttk.Radiobutton(mode_frame, text="ROI 搜索中心斑", variable=self.var_alignment_mode, value="roi").pack(side="left")
        ttk.Radiobutton(mode_frame, text="导入 alignment.mat", variable=self.var_alignment_mode, value="mat").pack(side="left", padx=(12, 0))

        row += 1
        ttk.Label(parent, text="alignment.mat:").grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=self.var_alignment_mat_path).grid(row=row, column=1, sticky="ew", pady=4)
        ttk.Button(parent, text="选择 .mat", command=self.choose_alignment_mat).grid(row=row, column=2, sticky="w", pady=4)
        ttk.Label(parent, text="变量名:").grid(row=row, column=3, sticky="w", padx=(12, 6), pady=4)
        ttk.Entry(parent, textvariable=self.var_alignment_mat_var, width=14).grid(row=row, column=4, sticky="w", pady=4)
        parent.columnconfigure(4, weight=1)

        row += 1
        ttk.Label(parent, text="MAT 列顺序:").grid(row=row, column=0, sticky="w", pady=4)
        ttk.Combobox(
            parent,
            textvariable=self.var_alignment_mat_columns,
            values=["dx_dy", "dy_dx"],
            state="readonly",
            width=12,
        ).grid(row=row, column=1, sticky="w", pady=4)
        ttk.Label(parent, text="含义: dx_dy=第1列X偏移, 第2列Y偏移").grid(row=row, column=2, columnspan=3, sticky="w", pady=4)

        row += 1
        ttk.Label(parent, text="参考 nav_y(可空):").grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=self.var_ref_nav_y, width=12).grid(row=row, column=1, sticky="w", pady=4)
        ttk.Label(parent, text="参考 nav_x(可空):").grid(row=row, column=2, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=self.var_ref_nav_x, width=12).grid(row=row, column=3, sticky="w", pady=4)

        row += 1
        ttk.Label(parent, text="平滑 sigma:").grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=self.var_smooth_sigma, width=12).grid(row=row, column=1, sticky="w", pady=4)
        ttk.Label(parent, text="局部精修半径:").grid(row=row, column=2, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=self.var_refine_radius, width=12).grid(row=row, column=3, sticky="w", pady=4)

        row += 1
        ttk.Label(parent, text="背景百分位:").grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=self.var_bg_percentile, width=12).grid(row=row, column=1, sticky="w", pady=4)
        ttk.Label(parent, text="ROI 信息:").grid(row=row, column=2, sticky="nw", pady=4)
        ttk.Label(parent, textvariable=self.var_roi_info, wraplength=420, justify="left").grid(row=row, column=3, columnspan=2, sticky="w", pady=4)

        row += 1
        ttk.Button(parent, text="选择 ROI", command=self.select_roi).grid(row=row, column=0, sticky="w", pady=8)
        ttk.Button(parent, text="清空 ROI", command=self.clear_roi).grid(row=row, column=1, sticky="w", pady=8)

        row += 1
        ttk.Checkbutton(parent, text="输出 shift 折线图", variable=self.var_save_shift_plot).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Checkbutton(parent, text="输出 shift CSV", variable=self.var_save_shift_csv).grid(row=row, column=1, sticky="w", pady=4)
        ttk.Checkbutton(parent, text="输出格式信息(JSON/TXT)", variable=self.var_save_format_info).grid(row=row, column=2, sticky="w", pady=4)

        row += 1
        ttk.Separator(parent, orient="horizontal").grid(row=row, column=0, columnspan=5, sticky="ew", pady=10)

        row += 1
        self.progress = ttk.Progressbar(parent, mode="determinate")
        self.progress.grid(row=row, column=0, columnspan=4, sticky="ew", pady=6)
        ttk.Label(parent, textvariable=self.var_progress_text).grid(row=row, column=4, sticky="w", pady=6)

        row += 1
        btns = ttk.Frame(parent)
        btns.grid(row=row, column=0, columnspan=5, sticky="w", pady=8)
        ttk.Button(btns, text="开始处理", command=self.start_processing).pack(side="left")
        ttk.Button(btns, text="退出", command=self.root.destroy).pack(side="left", padx=(8, 0))


    def _build_log_tab(self, parent):
        parent.rowconfigure(0, weight=1)
        parent.columnconfigure(0, weight=1)
        self.log_text = ScrolledText(parent, wrap="word", font=("Consolas", 10))
        self.log_text.grid(row=0, column=0, sticky="nsew")
        self.log_text.insert("end", f"{APP_TITLE} v{APP_VERSION}\n")
        self.log_text.insert("end", "准备就绪。\n")
        self.log_text.configure(state="disabled")

    def append_log(self, text):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def log(self, text):
        self.log_queue.put(("log", text))

    def set_progress(self, frac):
        self.log_queue.put(("progress", frac))

    def set_progress_text(self, text):
        self.log_queue.put(("progress_text", text))

    def _poll_log_queue(self):
        try:
            while True:
                kind, payload = self.log_queue.get_nowait()
                if kind == "log":
                    self.append_log(payload)
                elif kind == "progress":
                    self.progress["value"] = max(0, min(100, float(payload) * 100))
                elif kind == "progress_text":
                    self.var_progress_text.set(payload)
                elif kind == "done":
                    self.progress["value"] = 100
                    self.var_progress_text.set("完成")
                    messagebox.showinfo("完成", payload)
                elif kind == "error":
                    self.var_progress_text.set("失败")
                    messagebox.showerror("错误", payload)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_log_queue)

    def choose_input(self):
        path = filedialog.askopenfilename(
            title="选择 DM4 文件",
            filetypes=[("DM4 files", "*.dm4"), ("All files", "*.*")],
        )
        if path:
            self.var_input.set(path)
            if not self.var_output_basename.get().strip():
                self.var_output_basename.set(Path(path).stem)

    def choose_output_dir(self):
        path = filedialog.askdirectory(title="选择输出目录")
        if path:
            self.var_output_dir.set(path)

    def choose_alignment_mat(self):
        path = filedialog.askopenfilename(
            title="选择 alignment.mat 文件",
            filetypes=[("MAT files", "*.mat"), ("All files", "*.*")],
        )
        if path:
            self.var_alignment_mat_path.set(path)
            self.append_log(f"Selected alignment.mat: {path}")

    def parse_optional_int(self, text: str):
        text = text.strip()
        if text == "":
            return None
        return int(text)

    def load_shape(self):
        input_path = self.var_input.get().strip()
        if not input_path:
            messagebox.showwarning("提示", "请先选择 DM4 文件。")
            return
        try:
            signals, sig4, data = load_4d_stem_data(input_path, lazy=True)
            self.loaded_shape = tuple(int(v) for v in data.shape)
            self.var_shape_info.set(str(self.loaded_shape))
            del signals, sig4, data
            gc.collect()
            self.append_log(f"Loaded shape: {self.loaded_shape}")
        except Exception as e:
            messagebox.showerror("错误", str(e))

    def select_roi(self):
        input_path = self.var_input.get().strip()
        if not input_path:
            messagebox.showwarning("提示", "请先选择 DM4 文件。")
            return

        try:
            ref_y = self.parse_optional_int(self.var_ref_nav_y.get())
            ref_x = self.parse_optional_int(self.var_ref_nav_x.get())
            frame, actual_ref = get_reference_frame(input_path, nav_y=ref_y, nav_x=ref_x, lazy=self.var_lazy_load.get())
            self.var_ref_nav_y.set(str(actual_ref[0]))
            self.var_ref_nav_x.set(str(actual_ref[1]))
            roi = select_search_roi_interactive(
                frame,
                title=f"参考帧 nav={actual_ref}，拖拽框选 ROI，按 Enter 确认，Esc 取消",
            )
            if roi is None:
                self.append_log("ROI selection cancelled.")
                return
            self.selected_roi = roi
            self.var_roi_info.set(str(roi))
            self.append_log(f"Selected ROI: {roi}")
        except Exception as e:
            messagebox.showerror("错误", str(e))

    def clear_roi(self):
        self.selected_roi = None
        self.var_roi_info.set("未选择 ROI")

    def build_params(self) -> ProcessingParams:
        input_path = self.var_input.get().strip()
        output_dir = self.var_output_dir.get().strip()
        if not input_path:
            raise ValueError("请先选择输入 DM4 文件。")
        if not output_dir:
            raise ValueError("请先选择输出目录。")

        params = ProcessingParams(
            input_path=input_path,
            output_dir=output_dir,
            output_basename=self.var_output_basename.get().strip(),
            output_format=self.var_output_format.get().strip().lower(),
            bin_nav=int(self.var_bin_nav.get()),
            bin_sig=int(self.var_bin_sig.get()),
            out_row_chunk=int(self.var_out_row_chunk.get()),
            enable_alignment=bool(self.var_enable_alignment.get()),
            alignment_mode=self.var_alignment_mode.get().strip().lower(),
            alignment_mat_path=self.var_alignment_mat_path.get().strip(),
            alignment_mat_var=self.var_alignment_mat_var.get().strip() or "alignment",
            alignment_mat_columns=self.var_alignment_mat_columns.get().strip(),
            reference_nav_y=self.parse_optional_int(self.var_ref_nav_y.get()),
            reference_nav_x=self.parse_optional_int(self.var_ref_nav_x.get()),
            smooth_sigma=float(self.var_smooth_sigma.get()),
            refine_radius=int(self.var_refine_radius.get()),
            bg_percentile=float(self.var_bg_percentile.get()),
            subpixel=bool(self.var_subpixel.get()),
            roi=self.selected_roi,
            save_shift_plot=bool(self.var_save_shift_plot.get()),
            save_shift_csv=bool(self.var_save_shift_csv.get()),
            save_format_info=bool(self.var_save_format_info.get()),
            lazy_load=bool(self.var_lazy_load.get()),
        )

        if params.bin_nav <= 0 or params.bin_sig <= 0 or params.out_row_chunk <= 0:
            raise ValueError("bin 和 out row chunk 必须为正整数。")
        if params.output_format not in {"mrc", "img"}:
            raise ValueError("输出格式仅支持 mrc 或 img。")
        if params.enable_alignment:
            if params.alignment_mode == "roi" and params.roi is None:
                raise ValueError("alignment 模式为 ROI，请先选择 ROI。")
            if params.alignment_mode == "mat" and not params.alignment_mat_path:
                raise ValueError("alignment 模式为 MAT，请先选择 alignment.mat 文件。")
        return params

    def start_processing(self):
        if self.worker_thread is not None and self.worker_thread.is_alive():
            messagebox.showwarning("提示", "已有任务正在运行。")
            return

        try:
            params = self.build_params()
        except Exception as e:
            messagebox.showerror("参数错误", str(e))
            return

        self.progress["value"] = 0
        self.var_progress_text.set("处理中...")
        self.append_log("Starting processing...")

        def worker():
            logger = Logger(self.log)
            try:
                info = process_dm4_to_stack(params, logger, progress_cb=self.set_progress)
                msg = "处理完成。\n\n输出文件:\n" + "\n".join(info.get("generated_files", [info.get("output_path", "")]))
                self.log_queue.put(("done", msg))
            except Exception as e:
                tb = traceback.format_exc()
                self.log_queue.put(("log", tb))
                self.log_queue.put(("error", str(e)))

        self.worker_thread = threading.Thread(target=worker, daemon=True)
        self.worker_thread.start()


def main():
    root = tk.Tk()
    app = DM4ToolGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
