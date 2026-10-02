"""像素遮挡：在解码后的像素缓冲区上真正覆盖矩形，再重新编码。

入口只处理 *未压缩* 数据集（压缩像素由流水线提前判错拒绝）。
不依赖 pydicom ``pixel_array`` 的多帧维度约定（不同版本可能给出
(f,r,c)、(r,f,c) 或 (r,c,f)），而是直接基于
``Rows*Columns*SamplesPerPixel`` 的帧步长在扁平缓冲上按帧切片操作，
遮挡后按原始字节布局写回，保证：

* 覆盖逐帧生效（frames=None 时作用于全部帧）；
* 单色（MONOCHROME1/2、PALETTE COLOR）与交错 RGB/YBR_FULL 均可处理；
  PLANAR_CONFIGURATION=1 显式报错；
* 未命中遮挡的实例像素字节原样保留（字节一致的一部分）；
* 支持 8/16 位像素，字节序按传输语法判定。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageDraw
from pydicom.dataset import Dataset

from .issues import IssueList, PIXEL_ERROR
from .schema import Box

_SUPPORTED_PHOTOMETRICS = {
    "MONOCHROME1": 1,
    "MONOCHROME2": 1,
    "PALETTE COLOR": 1,
    "RGB": 3,
    "YBR_FULL": 3,
}

_BIG_ENDIAN_TS = "1.2.840.10008.1.2.2"


@dataclass
class OcclusionHit:
    """一条遮挡规则在一个实例上的执行结果（进入审计）。"""

    frame_index: int  # -1 表示所有帧
    x: int
    y: int
    width: int
    height: int
    pixels_changed: int


def _decode_pixel_buffer(ds: Dataset, issues: IssueList, file_name: str,
                         uid: str | None):
    """解码未压缩像素为扁平数组。

    返回 (flat, dtype, frame_size, nframes, spp, rows, cols)；
    任何像素结构问题登记到 issues 并返回 None。
    """
    photometric = str(getattr(ds, "PhotometricInterpretation",
                              "MONOCHROME2")).strip()
    spp = int(getattr(ds, "SamplesPerPixel", 1))
    expected_spp = _SUPPORTED_PHOTOMETRICS.get(photometric)
    if expected_spp is None or expected_spp != spp:
        issues.error(
            PIXEL_ERROR,
            f"不支持的像素结构: PhotometricInterpretation={photometric!r}, "
            f"SamplesPerPixel={spp}（仅支持未压缩单色与交错 RGB/YBR_FULL）",
            file=file_name, sop_instance_uid=uid,
        )
        return None
    if spp == 3 and int(getattr(ds, "PlanarConfiguration", 0)) != 0:
        issues.error(
            PIXEL_ERROR,
            "PlanarConfiguration=1（按平面排列的彩色像素）不支持遮挡",
            file=file_name, sop_instance_uid=uid,
        )
        return None
    for attr in ("Rows", "Columns", "BitsAllocated"):
        if not hasattr(ds, attr):
            issues.error(
                PIXEL_ERROR, f"像素描述标签 {attr} 缺失",
                file=file_name, sop_instance_uid=uid,
            )
            return None

    rows, cols = int(ds.Rows), int(ds.Columns)
    nframes = int(getattr(ds, "NumberOfFrames", 1) or 1)
    bits = int(ds.BitsAllocated)
    big_endian = bool(ds.file_meta) and \
        str(ds.file_meta.TransferSyntaxUID) == _BIG_ENDIAN_TS
    if bits == 8:
        dtype = np.dtype("u1")
    elif bits == 16:
        signed = int(getattr(ds, "PixelRepresentation", 0)) == 1
        dtype = np.dtype(">i2" if signed else ">u2") if big_endian \
            else np.dtype("<i2" if signed else "<u2")
    else:
        issues.error(
            PIXEL_ERROR,
            f"BitsAllocated={bits} 不支持遮挡（仅支持 8/16 位未压缩像素）",
            file=file_name, sop_instance_uid=uid,
        )
        return None

    frame_size = rows * cols * spp
    expected_count = frame_size * nframes
    raw = ds.PixelData
    if isinstance(raw, (list, tuple)):
        issues.error(
            PIXEL_ERROR, "PixelData 为封装片段，未压缩实例不应如此",
            file=file_name, sop_instance_uid=uid,
        )
        return None
    raw = bytes(raw or b"")
    if len(raw) < expected_count * dtype.itemsize:
        issues.error(
            PIXEL_ERROR,
            f"PixelData 长度 {len(raw)} 小于声明尺寸要求的 "
            f"{expected_count * dtype.itemsize}",
            file=file_name, sop_instance_uid=uid,
        )
        return None
    flat = np.frombuffer(raw, dtype=dtype, count=expected_count).copy()
    return flat, dtype, frame_size, nframes, spp, rows, cols


def _validate_fill(box: Box, dtype: np.dtype, issues: IssueList,
                   file_name: str, uid: str | None) -> bool:
    max_val = int(np.iinfo(dtype).max)
    if box.fill > max_val:
        issues.error(
            PIXEL_ERROR,
            f"遮挡填充值 {box.fill} 超过像素类型最大值 {max_val}",
            file=file_name, sop_instance_uid=uid,
        )
        return False
    return True


def apply_occlusions(ds: Dataset, rules: list[tuple[Box, list[int] | None]],
                     issues: IssueList, file_name: str,
                     uid: str | None) -> list[OcclusionHit]:
    """对单个实例应用全部命中的遮挡规则，就地改写 PixelData。

    成功返回审计命中记录；任何像素结构问题登记到 issues 并返回空列表。
    """
    if not rules:
        return []
    decoded = _decode_pixel_buffer(ds, issues, file_name, uid)
    if decoded is None:
        return []
    flat, dtype, frame_size, nframes, spp, rows, cols = decoded

    for box, _frames in rules:
        _validate_fill(box, dtype, issues, file_name, uid)
    if issues.has_errors:
        return []

    # 整帧的矩形遮罩只构造一次（所有规则共用相同 Rows/Columns）
    def mask_for(box: Box) -> tuple[np.ndarray, int]:
        x0, y0 = box.x, box.y
        x1, y1 = min(cols, box.x + box.width), min(rows, box.y + box.height)
        if x0 >= cols or y0 >= rows:
            return None, 0
        img = Image.new("L", (cols, rows), 0)
        ImageDraw.Draw(img).rectangle([x0, y0, x1 - 1, y1 - 1], fill=1)
        m2d = np.asarray(img, dtype=bool)
        count = int(m2d.sum())
        if spp == 3:
            # 交错像素：每个被选中的像素对应连续 3 个采样
            m2d = np.repeat(m2d[:, :, np.newaxis], 3, axis=2).reshape(-1)
        else:
            m2d = m2d.reshape(-1)
        return m2d, count

    hits: list[OcclusionHit] = []
    for box, frames in rules:
        if frames is None:
            target_frames = list(range(nframes))
            frame_index = -1
        else:
            bad = [f for f in frames if f < 0 or f >= nframes]
            if bad:
                issues.error(
                    PIXEL_ERROR,
                    f"遮挡帧号越界: {bad}（实例共 {nframes} 帧）",
                    file=file_name, sop_instance_uid=uid,
                )
                continue
            target_frames = frames
            frame_index = frames[0] if len(frames) == 1 else -1
        m, per_frame_count = mask_for(box)
        if m is None:
            issues.error(
                PIXEL_ERROR,
                f"遮挡框 ({box.x},{box.y},{box.width}x{box.height}) 完全位于 "
                f"{cols}x{rows} 图像之外",
                file=file_name, sop_instance_uid=uid,
            )
            continue
        for fi in target_frames:
            start = fi * frame_size
            flat[start:start + frame_size][m] = box.fill
        hits.append(OcclusionHit(
            frame_index=frame_index, x=box.x, y=box.y,
            width=box.width, height=box.height,
            pixels_changed=per_frame_count * len(target_frames),
        ))

    if issues.has_errors:
        return []
    ds.PixelData = np.ascontiguousarray(flat).tobytes()
    ds["PixelData"].is_undefined_length = False
    return hits
