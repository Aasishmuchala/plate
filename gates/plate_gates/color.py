"""Colour science for the gates: sRGB <-> CIE Lab (D65) and CIEDE2000.

Ported from the colour section of ~/Desktop/test/vizmaker-dossier/tools/fidelity_bench.py
(srgb_to_lab, hex_to_rgb8, ciede2000) plus the Lab -> sRGB inverse from
~/Desktop/test/car-entry/tools_labmatch.py. Numerics are unchanged: Bruce Lindbloom D65 matrix,
white point (0.95047, 1.0, 1.08883), Sharma/Wu/Dalal 2005 for CIEDE2000.
"""

from __future__ import annotations

import numpy as np

_M = np.array(
    [
        [0.4124564, 0.3575761, 0.1804375],
        [0.2126729, 0.7151522, 0.0721750],
        [0.0193339, 0.1191920, 0.9503041],
    ]
)
_M_INV = np.linalg.inv(_M)
_WHITE = np.array([0.95047, 1.0, 1.08883])  # D65
_D = 6 / 29


def srgb_to_lab(rgb8: np.ndarray) -> np.ndarray:
    """8-bit sRGB (..., 3) -> CIE Lab D65 (..., 3), float64."""
    rgb = np.asarray(rgb8).astype(np.float64) / 255.0
    lin = np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
    xyz = lin @ _M.T
    t = xyz / _WHITE
    f = np.where(t > _D**3, np.cbrt(t), t / (3 * _D * _D) + 4 / 29)
    L = 116 * f[..., 1] - 16
    a = 500 * (f[..., 0] - f[..., 1])
    b = 200 * (f[..., 1] - f[..., 2])
    return np.stack([L, a, b], -1)


def _finv(t: np.ndarray) -> np.ndarray:
    return np.where(t > _D, t**3, 3 * _D * _D * (t - 4 / 29))


def lab_to_srgb(lab: np.ndarray) -> np.ndarray:
    """CIE Lab D65 (..., 3) -> sRGB (..., 3) as float64 in [0, 255] (not rounded).

    Linear light is clipped to [0, 1] before the sRGB curve, as in tools_labmatch.
    """
    lab = np.asarray(lab, dtype=np.float64)
    L, A, B = lab[..., 0], lab[..., 1], lab[..., 2]
    fy = (L + 16) / 116
    fx = fy + A / 500
    fz = fy - B / 200
    xyz = np.stack([_finv(fx), _finv(fy), _finv(fz)], -1) * _WHITE
    lin = np.clip(xyz @ _M_INV.T, 0, 1)
    return np.where(lin <= 0.0031308, lin * 12.92, 1.055 * np.power(lin, 1 / 2.4) - 0.055) * 255.0


def hex_to_rgb8(h: str) -> np.ndarray:
    """'#RRGGBB' -> uint8 array of shape (1, 3)."""
    h = h.lstrip("#")
    return np.array([[int(h[i : i + 2], 16) for i in (0, 2, 4)]], dtype=np.uint8)


def ciede2000(lab1: np.ndarray, lab2: np.ndarray) -> np.ndarray:
    """CIEDE2000 colour difference, Sharma/Wu/Dalal 2005. Arrays (..., 3) broadcast."""
    L1, a1, b1 = lab1[..., 0], lab1[..., 1], lab1[..., 2]
    L2, a2, b2 = lab2[..., 0], lab2[..., 1], lab2[..., 2]
    C1 = np.hypot(a1, b1)
    C2 = np.hypot(a2, b2)
    Cb = (C1 + C2) / 2
    G = 0.5 * (1 - np.sqrt(Cb**7 / (Cb**7 + 25**7)))
    a1p = (1 + G) * a1
    a2p = (1 + G) * a2
    C1p = np.hypot(a1p, b1)
    C2p = np.hypot(a2p, b2)
    h1p = np.degrees(np.arctan2(b1, a1p)) % 360
    h2p = np.degrees(np.arctan2(b2, a2p)) % 360
    dLp = L2 - L1
    dCp = C2p - C1p
    dh = h2p - h1p
    dh = np.where(dh > 180, dh - 360, np.where(dh < -180, dh + 360, dh))
    dh = np.where((C1p * C2p) == 0, 0, dh)
    dHp = 2 * np.sqrt(C1p * C2p) * np.sin(np.radians(dh / 2))
    Lbp = (L1 + L2) / 2
    Cbp = (C1p + C2p) / 2
    hs = h1p + h2p
    hbp = np.where(
        (C1p * C2p) == 0,
        hs,
        np.where(np.abs(h1p - h2p) <= 180, hs / 2, np.where(hs < 360, (hs + 360) / 2, (hs - 360) / 2)),
    )
    T = (
        1
        - 0.17 * np.cos(np.radians(hbp - 30))
        + 0.24 * np.cos(np.radians(2 * hbp))
        + 0.32 * np.cos(np.radians(3 * hbp + 6))
        - 0.20 * np.cos(np.radians(4 * hbp - 63))
    )
    dth = 30 * np.exp(-(((hbp - 275) / 25) ** 2))
    Rc = 2 * np.sqrt(Cbp**7 / (Cbp**7 + 25**7))
    Sl = 1 + 0.015 * (Lbp - 50) ** 2 / np.sqrt(20 + (Lbp - 50) ** 2)
    Sc = 1 + 0.045 * Cbp
    Sh = 1 + 0.015 * Cbp * T
    Rt = -np.sin(np.radians(2 * dth)) * Rc
    return np.sqrt((dLp / Sl) ** 2 + (dCp / Sc) ** 2 + (dHp / Sh) ** 2 + Rt * (dCp / Sc) * (dHp / Sh))
