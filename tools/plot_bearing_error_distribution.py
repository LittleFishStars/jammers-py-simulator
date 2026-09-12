# -*- coding: utf-8 -*-
"""绘制「随机位置下示向度误差」的分布图像。

误差模型（官方 bearingnoise，spatial-bearing-v1）：
    E(位置, 频道) = Σ_{k=0..3} w_k · v_k
其中 v_k ~ U(-1,1) 为四个网格节点值（独立），w 为 smoothstep 双线性权重：
    w = [(1-sx)(1-sy), sx(1-sy), (1-sx)sy, sx·sy],  s = f²(3-2f),  f = frac(x/150)

位置随机 ⇒ (fx, fy) ~ U(0,1)² ⇒ E 的边际分布 = 对权重 w 的混合。

本脚本用「滑窗卷积递推」在网格上精确求每个相位条件下的密度（O(N)/权重，数值稳定），
再按相位求平均得到边际密度；尾部另用子集和闭式交叉校验。

输出：figures/bearing_error_distribution.png / .svg
运行：MPLCONFIGDIR=/tmp/mpl-cache python3 tools/plot_bearing_error_distribution.py
"""
from __future__ import annotations

import hashlib
import math
import os
from math import factorial, fsum

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

# ---------------------------------------------------------------- 字体
plt.rcParams["font.sans-serif"] = ["Noto Sans CJK JP", "Noto Sans CJK SC",
                                   "WenQuanYi Zen Hei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

GRID_M = 150.0          # 噪声网格间距（米），对应 bearing_noise_grid_um
N_BINS = 2048           # 密度网格分辨率
N_PHASE = 100           # 相位求积：每个方向 100 个中点


def smoothstep(t: np.ndarray | float):
    return t * t * (3.0 - 2.0 * t)


def weights_of(fx: float, fy: float) -> list[float]:
    """由单元内相位 (fx, fy) 得到 4 个双线性权重。"""
    sx, sy = float(smoothstep(fx)), float(smoothstep(fy))
    return [(1 - sx) * (1 - sy), sx * (1 - sy), (1 - sx) * sy, sx * sy]


def density_of_weights(w: list[float], n: int = N_BINS) -> np.ndarray:
    """加权均匀和 Σ wᵢ·Uᵢ(0,1) 的密度（格内质量，长度 n，支撑 [0,1]）。

    递推 f_k(x) = (1/w_k)·(F_{k-1}(x) - F_{k-1}(x-w_k))，用二次累积 J 做滑窗，
    数值稳定（无 Πw 相消）。权重 < 1 格时用确定性均值偏移近似（误差 < h/2）。
    """
    h = 1.0 / n
    shift = 0.0
    ws = []
    for x in w:
        if x < h:
            shift += x / 2.0
        else:
            ws.append(x)
    if not ws:
        m = np.zeros(n)
        i = min(n - 1, max(0, int(shift / h)))
        m[i] = 1.0
        return m
    edges = np.arange(n + 1) * h
    lo = np.clip(edges[:-1], 0.0, ws[0])
    hi = np.clip(edges[1:], 0.0, ws[0])
    m = np.maximum(0.0, hi - lo) / ws[0]
    z = edges
    for x in ws[1:]:
        F = np.concatenate(([0.0], np.cumsum(m)))              # CDF 于格点
        J = np.concatenate(([0.0], np.cumsum(h * (F[:-1] + F[1:]) / 2.0)))
        a = np.arange(n) * h
        b = a + h
        m = (np.interp(b, z, J) - np.interp(a, z, J)
             - np.interp(np.clip(b - x, 0, 1), z, J)
             + np.interp(np.clip(a - x, 0, 1), z, J)) / x
        np.maximum(m, 0.0, out=m)
    if shift > 0:
        k = int(shift / h)
        fr = shift / h - k
        m = (1 - fr) * np.roll(m, k) + fr * np.roll(m, k + 1)
    return m


# ---------------------------------------------------------------- 边际密度
def marginal_density():
    """在网格上求边际密度。返回 (e_grid, f_E(e), 相位条件σ列表)。"""
    n = N_BINS
    h = 1.0 / n
    half = 0.5 / N_PHASE
    phase = [(i + 0.5) / N_PHASE for i in range(N_PHASE)]
    acc = np.zeros(n)
    sig = []
    for fx in phase:
        for fy in phase:
            w = weights_of(fx, fy)
            acc += density_of_weights(w, n)
            sig.append(math.sqrt(sum(x * x for x in w) / 3.0))
    fz = acc / (N_PHASE * N_PHASE) / h          # 密度（非质量）
    # E = 2Z - 1：e = 2z-1 ⇒ f_E(e) = f_Z((e+1)/2)/2
    e = 2.0 * (np.arange(n) + 0.5) * h - 1.0
    return e, fz / 2.0, sig


# ---------------------------------------------------------------- 解析尾部
def analytic_tail(fx: float, fy: float, t: float) -> float:
    """P(|E|>t | 权重) = 2·P(ΣwᵢUᵢ < (1-t)/2)，子集和闭式（数值稳定）。"""
    if t <= 0.0:
        return 1.0
    d = (1.0 - t) / 2.0
    ws = [x for x in weights_of(fx, fy) if x > 1e-15]
    k = len(ws)
    if k == 0:
        return 0.0
    prod = 1.0
    for x in ws:
        prod *= x
    terms = []
    for mask in range(1 << k):
        s = fsum(ws[i] for i in range(k) if mask >> i & 1)
        u = d - s
        if u > 0.0:
            terms.append((1.0 if bin(mask).count("1") % 2 == 0 else -1.0) * u ** k)
    v = fsum(terms) / (factorial(k) * prod) if terms else 0.0
    return min(1.0, max(0.0, 2.0 * v))


def tail_curve(ts):
    phase = [(i + 0.5) / N_PHASE for i in range(N_PHASE)]
    out = []
    for t in ts:
        acc = fsum(analytic_tail(fx, fy, t) for fx in phase for fy in phase)
        out.append(min(1.0, acc / (N_PHASE * N_PHASE)))
    return np.array(out)


# ---------------------------------------------------------------- 噪声场
def noise_field(x0, y0, size_m, res_m, seed=0xEEEEEEEEEEEEEEEE, salt=7):
    """2D 误差场切片（演示 150m 相关长度）。"""
    def gv(ix, iy):
        dg = hashlib.blake2b(f"{seed}:{salt}:{ix}:{iy}".encode(), digest_size=8).digest()
        return 2.0 * int.from_bytes(dg, "big") / 2.0 ** 64 - 1.0

    def gvec(ixs, iy):
        """整行节点值：按 x 方向去重缓存，避免重复哈希。"""
        return np.array([gv(int(i), iy) for i in ixs])

    m = int(size_m / res_m)
    gx = (x0 + np.arange(m) * res_m) / GRID_M
    gy = (y0 + np.arange(m) * res_m) / GRID_M
    ix = np.floor(gx).astype(int)
    iy = np.floor(gy).astype(int)
    sx = smoothstep(gx - ix)
    sy = smoothstep(gy - iy)

    cache: dict[tuple[int, int], np.ndarray] = {}

    def column(j):
        key = (j, 0)
        if key not in cache:
            cache[key] = np.array([gv(int(k), j) for k in range(ix[0], ix[-1] + 2)])
        off = ix - ix[0]
        return cache[key][off], cache[key][off + 1]

    field = np.zeros((m, m))
    for r in range(m):
        j = int(iy[r])
        v00, v10 = column(j)
        v01, v11 = column(j + 1)
        near = v00 + sx * (v10 - v00)
        far = v01 + sx * (v11 - v01)
        field[r] = near + sy[r] * (far - near)
    return field


# ---------------------------------------------------------------- 主流程
def main():
    out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "figures")
    os.makedirs(out_dir, exist_ok=True)

    print("计算边际密度（相位求积 %d×%d，网格 %d）…" % (N_PHASE, N_PHASE, N_BINS))
    e, fE, sig = marginal_density()
    s_mean = float(np.mean(sig))
    print(f"  条件 σ 范围 {min(sig):.4f} ~ {max(sig):.4f}，均值 {s_mean:.4f}")

    # 矩与累积校验
    var = float(np.sum(fE * e**2) * (e[1] - e[0]))
    sd = math.sqrt(var)
    kurt = float(np.sum(fE * e**4) * (e[1] - e[0])) / var**2
    mass = float(np.sum(fE) * (e[1] - e[0]))
    print(f"  归一化 = {mass:.6f}   σ = {sd:.4f}   峰度 = {kurt:.4f}   f(0) = {fE[len(fE)//2]:.4f}")
    # 理论值：E[Σw²]=0.551837（解析），E[E⁴]=E[Σw²]²/3-(2/15)E[Σw⁴]
    p_ = 1 - 2 * 0.5 + 2 * (9 / 5 - 2 + 4 / 7)      # E[s²+(1-s)²] = 0.742857
    print(f"  理论   σ = {math.sqrt(p_*p_/3):.4f}   峰度 = 2.2215（截面解析）")

    def surv(t):
        return float(np.sum(fE[e > t]) * (e[1] - e[0])) * 2.0

    print("  累积校验（密度积分 vs 解析闭式）：")
    for t in (0.5, 0.7, 0.9):
        a = surv(t)
        b = float(tail_curve([t])[0])
        print(f"    P(|E|>{t}) 密度积分={a:.5f}  解析={b:.5f}  相对差 {a/b-1:+.2%}")

    # ---------------- 画图 ----------------
    fig = plt.figure(figsize=(15.5, 10.5), dpi=120)
    gs = fig.add_gridspec(2, 2, hspace=0.30, wspace=0.24,
                          left=0.07, right=0.975, top=0.90, bottom=0.08)
    fig.suptitle("随机位置下示向度误差 $E$ 的分布（官方 spatial-bearing-v1 空间噪声）",
                 fontsize=16.5, fontweight="bold", y=0.965)

    # ---- A. 密度对比
    ax = fig.add_subplot(gs[0, 0])
    from statistics import NormalDist
    nd = NormalDist(0.0, sd)
    pdf_n = np.array([nd.pdf(v) for v in e])
    ax.fill_between(e, 0, fE, color="#2b6cb0", alpha=0.30)
    ax.plot(e, fE, color="#1a365d", lw=2.6, label="精确边际密度（本模型）")
    ax.plot(e, pdf_n, color="#c53030", lw=1.9, ls="--", label=f"正态分布 N(0, {sd:.3f}²)")
    ax.axhline(0.5, color="#718096", lw=1.6, ls=":", label="均匀分布 U(−1°, 1°)")
    for s in (-1, 1):
        ax.axvline(s, color="#444", lw=1.2)
    ax.axvspan(-1, 1, color="#f6e05e", alpha=0.10)
    ax.annotate("严格边界 ±1°\n密度二次归零", xy=(1.0, 0.02), xytext=(0.52, 0.62),
                fontsize=10, color="#744210",
                arrowprops=dict(arrowstyle="->", color="#b7791f", lw=1.3))
    ax.axvline(-sd, color="#2b6cb0", lw=0.9, alpha=0.5)
    ax.axvline(sd, color="#2b6cb0", lw=0.9, alpha=0.5)
    ax.set_title("(a) 密度形态：平顶、无正态尖峰、边界截断", fontsize=12.5)
    ax.set_xlabel("示向度误差 $E$ (°)", fontsize=11)
    ax.set_ylabel("概率密度", fontsize=11)
    ax.set_xlim(-1.28, 1.28)
    ax.set_ylim(0, 1.02)
    ax.legend(fontsize=9.5, loc="upper left", framealpha=0.95)
    ax.grid(alpha=0.25)
    ax.text(0.98, 0.97,
            f"σ = {sd:.3f}°\n峰度 = {kurt:.2f}（正态 3.00）\nf(0) = {fE[len(fE)//2]:.3f}（正态 {nd.pdf(0):.3f}）",
            transform=ax.transAxes, ha="right", va="top", fontsize=9.5,
            bbox=dict(boxstyle="round", fc="#fffbea", ec="#d69e2e"))

    # ---- B. 尾部
    ax = fig.add_subplot(gs[0, 1])
    ts = np.linspace(0.0, 0.999, 220)
    ex = tail_curve(ts)
    nz = 2 * (1 - np.array([nd.cdf(t / sd) for t in ts]))
    law = 1.125 * (1 - ts) ** 2
    ax.semilogy(ts, ex, color="#1a365d", lw=2.6, label="精确 $P(|E|>t)$（子集和闭式）")
    ax.semilogy(ts, nz, color="#c53030", lw=1.9, ls="--", label="正态同 σ 近似")
    ax.semilogy(ts, law, color="#38a169", lw=1.7, ls="-.", label=r"$\frac{9}{8}(1-t)^2$ 尾部律")
    q95 = 0.791
    q99 = 0.907
    for q, lb in ((q95, "95%"), (q99, "99%")):
        ax.axvline(q, color="#805ad5", lw=1.0, ls=":")
        ax.text(q, 1.4e-4, f" {lb}分位 {q:.2f}°", color="#553c9a", fontsize=9, rotation=90,
                va="bottom")
    ax.set_ylim(1e-4, 1.3)
    ax.set_xlim(0, 1.02)
    ax.set_title("(b) 尾概率：正态近似在高分位严重高估", fontsize=12.5)
    ax.set_xlabel("阈值 $t$ (°)", fontsize=11)
    ax.set_ylabel("$P(|E|>t)$（对数）", fontsize=11)
    ax.legend(fontsize=9.5, loc="lower left", framealpha=0.95)
    ax.grid(alpha=0.25, which="both")
    ax.text(0.97, 0.95, "99% 分位：精确 0.91°\n正态近似 1.11°（高估 22%）",
            transform=ax.transAxes, ha="right", va="top", fontsize=9.5,
            bbox=dict(boxstyle="round", fc="#f0fff4", ec="#38a169"))

    # ---- C. 尺度混合
    ax = fig.add_subplot(gs[1, 0])
    for c, col in ((0.001, "#a0aec0"), (0.12, "#90cdf4"), (0.3, "#4299e1"), (0.5, "#2b6cb0")):
        w = weights_of(c, c)
        m = density_of_weights(w)
        h = 1.0 / N_BINS
        z = (np.arange(N_BINS) + 0.5) * h
        ee = 2 * z - 1
        s_c = math.sqrt(sum(x * x for x in w) / 3.0)
        ax.plot(ee, m / h / 2, lw=1.5, color=col, alpha=0.9,
                label=f"单元内 f={c:.3g} → σ={s_c:.3f}°")
    ax.plot(e, fE, color="#1a365d", lw=3.0, label="混合后（随机位置）")
    ax.set_title("(c) 本质是尺度混合：位置决定噪声强度 σ∈[0.289°, 0.577°]", fontsize=12.5)
    ax.set_xlabel("示向度误差 $E$ (°)", fontsize=11)
    ax.set_ylabel("概率密度", fontsize=11)
    ax.set_xlim(-1.15, 1.15)
    ax.set_ylim(0, 1.35)
    ax.legend(fontsize=9, loc="upper left", framealpha=0.95)
    ax.grid(alpha=0.25)
    ax.annotate("网格节点上：退化为\n均匀分布（最宽）", xy=(-0.02, 0.50), xytext=(-1.08, 1.06),
                fontsize=9.5, color="#4a5568",
                arrowprops=dict(arrowstyle="->", color="#a0aec0", lw=1.2))
    ax.annotate("单元中心：Bates(4) 型\n钟形（最窄）", xy=(0.0, 1.17), xytext=(0.30, 1.20),
                fontsize=9.5, color="#2b6cb0",
                arrowprops=dict(arrowstyle="->", color="#2b6cb0", lw=1.2))

    # ---- D. 空间相关
    ax = fig.add_subplot(gs[1, 1])
    field = noise_field(0.0, 0.0, 700.0, 3.0)
    im = ax.imshow(field, extent=(0, 700, 0, 700), origin="lower", cmap="RdBu_r",
                   vmin=-1, vmax=1, interpolation="bilinear")
    for v in range(0, 701, 150):
        ax.axvline(v, color="#1a202c", lw=0.8, alpha=0.55)
        ax.axhline(v, color="#1a202c", lw=0.8, alpha=0.55)
    cb = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.03)
    cb.set_label("该点示向度误差 (°)", fontsize=10)
    ax.set_title("(d) 误差场是光滑空间场：相关长度 ≈ 150 m", fontsize=12.5)
    ax.set_xlabel("x (m)", fontsize=11)
    ax.set_ylabel("y (m)", fontsize=11)
    ax.text(0.98, 0.03, "黑线 = 150 m 噪声网格\n同格内误差同向，多点平均无增益",
            transform=ax.transAxes, ha="right", va="bottom", fontsize=9.5,
            bbox=dict(boxstyle="round", fc="white", ec="#4a5568", alpha=0.92))

    png = os.path.join(out_dir, "bearing_error_distribution.png")
    svg = os.path.join(out_dir, "bearing_error_distribution.svg")
    fig.savefig(png, bbox_inches="tight", facecolor="white")
    fig.savefig(svg, bbox_inches="tight", facecolor="white")
    print("已保存：", png)
    print("已保存：", svg)


if __name__ == "__main__":
    main()
