# -*- coding: utf-8 -*-
"""倒圆角半径缩小（截面周长守恒）—— 管件外形编辑模块 (corner_shrink.py)

功能
----
把管件截面上**小半径的倒圆角边**（四角那几段圆角弧面）的半径缩小 dR（默认 2 mm），
让两侧相邻的大面"自然延伸"接上去，并保证**每个截面的周长严格不变**，输出新的管件 STEP。

几何规则（A 方案，已与需求方确认）
--------------------------------
"半径严格 −dR"与"周长严格不变"不能靠"面完全不动"同时做到（面不动时周长会变长）。
本模块的做法：
  1. 每个倒圆角：半径改为 (R − dR)/s，用 2D Newton 解圆心，使其与前后两条邻面曲线
     **保持相切**；相邻面曲面本身不动，只改参数范围（即"延伸/裁剪"）；
  2. 整个截面绕其形心**等比缩放 s** —— 缩放保相切，于是倒圆半径最后正好 = R − dR；
  3. s 由"缩放后周长 = 原周长"迭代解出（2~3 次收敛，实测残差 < 1e-9 mm）。

等价比：四个面各内移
    δ ≈ dR · Σ_i[2·tan(θ_i/2) − θ_i] / (2π)
（θ_i = 各倒圆的转角）。4 个 90° 角、dR=2 时 δ≈0.55 mm；
1324-1 那种 47.5° 浅角，δ≈0.03 mm。

三维重建（沿**局部轴线**）
--------------------------
管件常在末端弯曲/收口，垂直于 X 的截面在弯段是斜切，不能直接放样。本模块：
  1. 用"每站环形截面形心"拟合**中心轴 spine**（光滑 BSpline）；
  2. 在 spine 的**法向截面**上判断真实截面是否恒定（这才是"每个截面"）；
  3. 真实截面恒定 → 只重建**一条**修改后的截面，沿 spine **扫掠**（MakePipeShell）；
     不恒定 → 逐站法向截面上重建，再 ThruSections 放样。

用法
----
    from corner_shrink import analyze, shrink
    info = analyze("part.stp")                  # 只读分析：倒圆位置/半径/截面恒定否
    res  = shrink("part.stp", dR=2.0)           # 生成新管件
命令行:
    python corner_shrink.py <part.stp> [dR] [--analyze]
"""

from __future__ import annotations

import math
import os
import time
from typing import Dict, List, Optional, Tuple

import numpy as np

from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.BRepBuilderAPI import (BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeFace,
                                BRepBuilderAPI_MakeSolid, BRepBuilderAPI_MakeWire,
                                BRepBuilderAPI_Sewing, BRepBuilderAPI_Transform,
                                BRepBuilderAPI_Transformed)
from OCP.BRepGProp import BRepGProp
from OCP.BRep import BRep_Builder
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.BRepOffsetAPI import (BRepOffsetAPI_MakePipeShell, BRepOffsetAPI_MakeThickSolid,
                                BRepOffsetAPI_ThruSections)
from OCP.BRepTools import BRepTools_WireExplorer
from OCP.GC import GC_MakeArcOfCircle
from OCP.GCPnts import GCPnts_QuasiUniformAbscissa
from OCP.GeomAbs import GeomAbs_C2, GeomAbs_Circle, GeomAbs_Plane
from OCP.GeomAPI import GeomAPI_PointsToBSpline, GeomAPI_ProjectPointOnCurve
from OCP.GProp import GProp_GProps
from OCP.TColgp import TColgp_Array1OfPnt
from OCP.TopTools import TopTools_ListOfShape
from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED, TopAbs_SHELL
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS, TopoDS_Compound
from OCP.gp import gp_Ax2, gp_Dir, gp_Pln, gp_Pnt, gp_Trsf, gp_Vec

from cavity_cutter import bool_cut
from errors import MoldGenerationError
from geometry_utils import is_valid_shape, shape_bbox, shape_volume
from model_reader import read_product_model
from pipe_mold import section_wires_plane

__all__ = ["analyze", "shrink", "plane_basis"]


# =============================================================== 基础度量 / 平面框架
def wire_len(w) -> float:
    g = GProp_GProps(); BRepGProp.LinearProperties_s(w, g)
    return float(g.Mass())


def wire_center(w):
    g = GProp_GProps(); BRepGProp.LinearProperties_s(w, g)
    return g.CentreOfMass()


def wire_area(w) -> float:
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    f = BRepBuilderAPI_MakeFace(w).Face()
    g = GProp_GProps(); BRepGProp.SurfaceProperties_s(f, g)
    return float(g.Mass())


def plane_basis(origin, normal) -> Tuple[gp_Pnt, np.ndarray, np.ndarray]:
    """构造截面平面的 2D 基（u, v），返回 (origin, u, v) 三个 3D 量。"""
    n = np.array([normal[0], normal[1], normal[2]], dtype=float)
    n /= np.linalg.norm(n)
    ref = np.array([0.0, 0.0, 1.0]) if abs(n[2]) < 0.9 else np.array([1.0, 0.0, 0.0])
    u = np.cross(ref, n); u /= np.linalg.norm(u)
    v = np.cross(n, u); v /= np.linalg.norm(v)
    return gp_Pnt(float(origin[0]), float(origin[1]), float(origin[2])), u, v


def _to_uv(O, u, v, P) -> np.ndarray:
    d = np.array([P.X() - O.X(), P.Y() - O.Y(), P.Z() - O.Z()])
    return np.array([float(np.dot(d, u)), float(np.dot(d, v))])


def _to_3d(O, u, v, uv) -> gp_Pnt:
    P = np.array([O.X(), O.Y(), O.Z()]) + uv[0] * u + uv[1] * v
    return gp_Pnt(float(P[0]), float(P[1]), float(P[2]))


# =============================================================== 截面边信息
def edge_items(wire) -> List[dict]:
    out, ex = [], BRepTools_WireExplorer(wire)
    while ex.More():
        e = ex.Current(); ex.Next()
        ad = BRepAdaptor_Curve(e)
        u0, u1 = ad.FirstParameter(), ad.LastParameter()
        out.append({"edge": e, "curve": ad.Curve().Curve(),
                    "lo": min(u0, u1), "hi": max(u0, u1),
                    "rev": e.Orientation() == TopAbs_REVERSED,
                    "type": ad.GetType(),
                    "R": (ad.Circle().Radius() if ad.GetType() == GeomAbs_Circle else None)})
    return out


def _proj(curve, P):
    api = GeomAPI_ProjectPointOnCurve(P, curve)
    if api.NbPoints() == 0:
        return float("inf"), None, None
    return float(api.LowerDistance()), api.NearestPoint(), float(api.LowerDistanceParameter())


def corner_radius_threshold(area: float, ratio: float = 0.30) -> float:
    """倒圆判定阈值：半径 < ratio·√面积。1324-1：√2443=49.4 → 14.83 mm，恰好抓住 R=9.93。"""
    return ratio * math.sqrt(max(area, 1e-9))


# =============================================================== 2D 重建
def _arc_mid(C, PA, PB, R, orig_mid):
    ax = np.array([PA.X() - C.X(), PA.Y() - C.Y(), PA.Z() - C.Z()]); ax /= np.linalg.norm(ax)
    bx = np.array([PB.X() - C.X(), PB.Y() - C.Y(), PB.Z() - C.Z()]); bx /= np.linalg.norm(bx)
    vec = ax + bx
    if np.linalg.norm(vec) < 1e-9:
        raise MoldGenerationError("倒圆弧接近 180°，中点退化", stage="corner_shrink")
    m = vec / np.linalg.norm(vec)
    om = np.array([orig_mid.X(), orig_mid.Y(), orig_mid.Z()])
    best, bd = None, None
    for sg in (1.0, -1.0):
        P = gp_Pnt(C.X() + sg * R * m[0], C.Y() + sg * R * m[1], C.Z() + sg * R * m[2])
        d = math.dist((P.X(), P.Y(), P.Z()), om)
        if best is None or d < bd:
            best, bd = P, d
    return best


def _refit_corner(pv, co, nx, Rp, frame):
    """把倒圆半径改成 Rp，解圆心 C（在截面平面内）使 |C−邻边A| = |C−邻边B| = Rp。"""
    O, u, v = frame
    ad = BRepAdaptor_Curve(co["edge"])
    C0 = ad.Circle().Location()
    uv = _to_uv(O, u, v, C0)

    def F(uvv):
        P = _to_3d(O, u, v, uvv)
        return np.array([_proj(pv["curve"], P)[0] - Rp, _proj(nx["curve"], P)[0] - Rp])

    h = 1e-5
    for _ in range(80):
        f = F(uv)
        if np.max(np.abs(f)) < 1e-12:
            break
        J = np.zeros((2, 2))
        for k in range(2):
            d = np.zeros(2); d[k] = h
            J[:, k] = (F(uv + d) - f) / h
        try:
            uv = uv + np.linalg.solve(J, -f)
        except np.linalg.LinAlgError as exc:
            raise MoldGenerationError(f"倒圆重建失败（Jacobian 奇异）: {exc}",
                                      stage="corner_shrink") from exc
    res = float(np.max(np.abs(F(uv))))
    if res > 1e-7:
        raise MoldGenerationError(f"倒圆重建未收敛（残差 {res:.2e}）", stage="corner_shrink")
    Pc = _to_3d(O, u, v, uv)
    _, PA, uA = _proj(pv["curve"], Pc)
    _, PB, uB = _proj(nx["curve"], Pc)
    orig_mid = ad.Value(0.5 * (co["lo"] + co["hi"]))
    arc = GC_MakeArcOfCircle(PA, _arc_mid(Pc, PA, PB, Rp, orig_mid), PB).Value()
    return arc, uA, uB


def rebuild_section(wire, dR: float, r_thr: float, frame, s_scale: float = 1.0):
    """重建一条截面：倒圆半径 (R−dR)/s，邻边只改参数范围。返回 (wire 或 None, plans)。"""
    items = edge_items(wire)
    n = len(items)
    corners = [i for i, x in enumerate(items)
               if x["type"] == GeomAbs_Circle and x["R"] and x["R"] < r_thr]
    if not corners:
        return None, {}

    plans: Dict[int, dict] = {}
    for i in corners:
        Rp = (items[i]["R"] - dR) / s_scale
        if Rp <= 1e-3:
            raise MoldGenerationError(
                f"倒圆半径 {items[i]['R']:.3f} 减 {dR} 后过小（{Rp:.3f}）",
                stage="corner_shrink")
        ip, inx = (i - 1) % n, (i + 1) % n
        arc, uA, uB = _refit_corner(items[ip], items[i], items[inx], Rp, frame)
        plans[i] = {"arc": arc, "uA": uA, "uB": uB, "ip": ip, "in": inx,
                    "R_old": items[i]["R"]}

    touched = set()
    for i in corners:
        touched.add((i - 1) % n)
        touched.add((i + 1) % n)

    edges_out = []
    for i, x in enumerate(items):
        if i in plans:
            edges_out.append(BRepBuilderAPI_MakeEdge(plans[i]["arc"]).Edge())
            continue
        if i not in touched:                     # 不动的边：原样复用，保住共享顶点
            edges_out.append(x["edge"])
            continue
        a, b, rev = x["lo"], x["hi"], x["rev"]
        pp, np_ = plans.get((i - 1) % n), plans.get((i + 1) % n)
        if pp is not None and pp["in"] == i:
            if rev: b = pp["uB"]
            else:   a = pp["uB"]
        if np_ is not None and np_["ip"] == i:
            if rev: a = np_["uA"]
            else:   b = np_["uA"]
        e = BRepBuilderAPI_MakeEdge(x["curve"], a, b).Edge()
        if rev:
            e.Orientation(TopAbs_REVERSED)
        edges_out.append(e)

    mw = BRepBuilderAPI_MakeWire()
    for e in edges_out:
        mw.Add(e)
    if not mw.IsDone():
        return None, plans
    return mw.Wire(), plans


def solve_section(wire, dR: float, r_thr: float, frame,
                  tol: float = 1e-9, itmax: int = 6):
    """解缩放 s，使 s × 重建周长 = 原周长。返回 (新 wire 或 None, s, 残差 mm)。"""
    P0 = wire_len(wire)
    s, w2, err = 1.0, None, float("nan")
    for _ in range(itmax):
        w2, _ = rebuild_section(wire, dR, r_thr, frame, s_scale=s)
        if w2 is None:
            return None, s, float("nan")
        P1 = wire_len(w2) * s
        err = P1 - P0
        if abs(err) < tol:
            return w2, s, err
        s *= P0 / P1
    return w2, s, err


def _scale_wire(wire, s: float, center):
    tr = gp_Trsf()
    tr.SetScale(gp_Pnt(center.X(), center.Y(), center.Z()), s)
    return BRepBuilderAPI_Transform(wire, tr, True, False).Shape()


def _signed_area(wire, normal):
    """截面环在法向平面内的有向面积（用来判断"向内/向外"）。"""
    u, v, _n = _frame_axes(normal)
    pts = []
    for it in edge_items(wire):
        for j in range(6):
            t = it["lo"] + (it["hi"] - it["lo"]) * j / 6.0
            p = it["curve"].Value(t)
            pts.append((float(np.dot([p.X(), p.Y(), p.Z()], u)),
                        float(np.dot([p.X(), p.Y(), p.Z()], v))))
    a = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        a += x1 * y2 - x2 * y1
    return 0.5 * a


def _offset_wire_in_plane(wire, normal, delta):
    """在截面平面内把闭合 wire 偏移 delta（负=向内），返回偏移后的 wire。

    用 2D 偏移（BRepOffsetAPI_MakeOffset）而不是 3D 抽壳：本件上 3D 的
    MakeThickSolid 对放样出的 BSpline 实体直接失败，而平面内 2D 偏移很稳。
    """
    from OCP.BRepOffsetAPI import BRepOffsetAPI_MakeOffset

    a0 = abs(_signed_area(wire, normal))

    def _once(d):
        try:
            mk = BRepOffsetAPI_MakeOffset(TopoDS.Wire_s(wire), GeomAbs_Arc, False)
            mk.Perform(d)
            if not mk.IsDone():
                return None
            sh = mk.Shape()
            cans = []
            ex = TopExp_Explorer(sh, TopAbs_WIRE)
            while ex.More():
                cans.append(TopoDS.Wire_s(ex.Current())); ex.Next()
            if not cans:
                # 闭合 wire 的偏移通常返回"一堆边"，自己拼回环
                from pipe_mold import _assemble_wires
                edges = []
                ex2 = TopExp_Explorer(sh, TopAbs_EDGE)
                while ex2.More():
                    edges.append(TopoDS.Edge_s(ex2.Current())); ex2.Next()
                cans = _assemble_wires(edges, tol=1e-3) if edges else []
            if not cans:
                return None
            # 取面积最接近 a0 的那条（向外偏移会变大，向内会变小）
            cans.sort(key=lambda w: abs(abs(_signed_area(w, normal)) - a0))
            return normalize_wire(cans[0], normal)
        except Exception:  # noqa: BLE001
            return None

    for d in (delta, -delta):
        w = _once(d)
        if w is not None and abs(_signed_area(w, normal)) < a0:
            return w
    raise MoldGenerationError("截面 2D 偏移失败（无法向内偏移一个壁厚）",
                              stage="corner_shrink")


def normalize_wire(wire, normal):
    """把截面环规范化：**统一起始边** + **统一绕向**，供 ThruSections 逐边对应。

    ThruSections 要求一串截面的边一一对应；而 BRepAlgoAPI_Section 在不同站位给出的
    边起点、以及内孔环的绕向都可能不同 —— 实测外轮廓能放样、内孔直接失败。
    做法：以环心为原点算各边起点的极角，取最小的那条作为第 0 条；再按有向面积统一绕向。
    """
    u, v, _n = _frame_axes(normal)
    items = edge_items(wire)
    if len(items) < 2:
        return wire
    c = wire_center(wire)
    O = np.array([c.X(), c.Y(), c.Z()])

    def _ang(it):
        p = it["curve"].Value(it["lo"] if not it["rev"] else it["hi"])
        d = np.array([p.X(), p.Y(), p.Z()]) - O
        return math.atan2(float(np.dot(d, v)), float(np.dot(d, u)))

    k = min(range(len(items)), key=lambda i: _ang(items[i]))
    ordered = items[k:] + items[:k]
    mw = BRepBuilderAPI_MakeWire()
    for it in ordered:
        mw.Add(it["edge"])
    w2 = TopoDS.Wire_s(mw.Shape()) if mw.IsDone() else wire

    # 绕向：按有向面积统一成同一符号
    pts = []
    for it in edge_items(w2):
        for j in range(6):
            t = it["lo"] + (it["hi"] - it["lo"]) * j / 6.0
            p = it["curve"].Value(t)
            d = np.array([p.X(), p.Y(), p.Z()]) - O
            pts.append((float(np.dot(d, u)), float(np.dot(d, v))))
    if len(pts) < 3:
        return w2
    a = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        a += x1 * y2 - x2 * y1
    return w2 if a >= 0 else TopoDS.Wire_s(w2.Reversed())


def _frame_axes(T, ref=(0.0, 1.0, 0.0)):
    """与 gp_Ax2(P, N=T, Vx=ref) 一致的局部三轴 (u, v, n)。"""
    n = np.array(T, dtype=float)
    n /= max(np.linalg.norm(n), 1e-12)
    r = np.array(ref, dtype=float)
    u = r - n * float(np.dot(r, n))            # ref 在法平面内的投影
    if np.linalg.norm(u) < 1e-9:
        r = np.array([1.0, 0.0, 0.0])
        u = r - n * float(np.dot(r, n))
    u /= np.linalg.norm(u)
    v = np.cross(n, u)                          # 与 gp_Ax2 的 Y = Z × X 一致
    return u, v, n


def _place_frame(shape, P0, T0, Pe, Te, ref=(0.0, 1.0, 0.0)):
    """把截面从「帧(P0, T0)」刚体搬到「帧(Pe, Te)」：用来算扫掠体的端面。"""
    u0, v0, n0 = _frame_axes(T0, ref)
    ue, ve, ne = _frame_axes(Te, ref)
    R = np.column_stack([ue, ve, ne]) @ np.column_stack([u0, v0, n0]).T
    t = (np.array([Pe.X(), Pe.Y(), Pe.Z()], dtype=float)
         - R @ np.array([P0.X(), P0.Y(), P0.Z()], dtype=float))
    tr = gp_Trsf()
    tr.SetValues(float(R[0, 0]), float(R[0, 1]), float(R[0, 2]), float(t[0]),
                 float(R[1, 0]), float(R[1, 1]), float(R[1, 2]), float(t[1]),
                 float(R[2, 0]), float(R[2, 1]), float(R[2, 2]), float(t[2]))
    return BRepBuilderAPI_Transform(shape, tr, True, False).Shape()


# =============================================================== 中心轴
def ring_centroid(wo, wi, ao, ai):
    """环形（外轮廓 − 内孔）的面积形心 —— 比单取外轮廓形心更贴近真实轴线。"""
    co, ci = wire_center(wo), wire_center(wi)
    w = max(ao - ai, 1e-9)
    return ((ao * co.X() - ai * ci.X()) / w,
            (ao * co.Y() - ai * ci.Y()) / w,
            (ao * co.Z() - ai * ci.Z()) / w)


def trace_spine(shape, x0, x1, step: float = 12.0, max_iter: int = 400):
    """沿管件**逐步推进**地追踪中心轴（每步都做法向截面，抗弯折）。

    做法：
      1. 起点取 −X 端法向截面（初始法向 = +X）的环形形心；
      2. 每步以当前轴点 P、当前切向 T 剖一个法向截面，取环形形心作为新轴点；
      3. 切向由相邻两个轴点之差更新（弯折段自然跟随）；
      4. 截面面积骤降（< 85% 参考面积）= 已经切到封口/斜切端 → 停止。

    这比"用 X 向截面形心拟合"可靠得多：X 向截面在 30° 弯折段是斜切，
    面积会虚大 1/cosθ、形心也会漂出去（实测末端 Z 只跟到 10.9 mm，而材料到 49.6 mm）。
    """
    first = section_wires_plane(shape, (x0 + 2.0, 0.0, 0.0), (1.0, 0.0, 0.0))
    if len(first) < 2:
        raise MoldGenerationError("起点截面剖切失败", stage="corner_shrink")
    c0 = ring_centroid(first[0][0], first[1][0], first[0][1], first[1][1])
    ref_area = first[0][1]
    P = np.array(c0, dtype=float)
    T = np.array([1.0, 0.0, 0.0])
    pts = [tuple(P)]
    prev = None
    for _ in range(max_iter):
        if P[0] > x1 - 1.0:
            break
        ws = section_wires_plane(shape, tuple(P), tuple(T))
        if len(ws) < 2:
            break
        (wo, ao), (wi, ai) = ws[0], ws[1]
        if ao < 0.85 * ref_area:            # 切到封口了
            break
        c = np.array(ring_centroid(wo, wi, ao, ai), dtype=float)
        if np.linalg.norm(c - P) > 2.0:     # 轴点跳变 ⇒ 截面已不完整
            break
        # 切向只用"相隔一整步"的两个轴点之差来估：相邻两次的形心几乎重合，
        # 拿它们之差归一化会把方向放大成噪声，下一步就会切歪（实测 4 步内就崩）。
        if prev is not None:
            d = c - prev
            n = np.linalg.norm(d)
            if n > 1e-6:
                T = d / n
        pts.append(tuple(c))
        prev = c
        P = c + step * T
    if len(pts) < 4:
        raise MoldGenerationError("中心轴追踪失败：有效轴点太少", stage="corner_shrink")
    # 端部外延：追踪在"截面面积骤降"处就停了（端口是斜切/收口），但管件本体还往前延伸。
    # 这里必须**严格限步 + 强制沿 X**：早先的版本按"相邻两点之差"外延，方向一旦跑偏成
    # Y/Z 主导，X 每步只走 0.01 mm 而 Y/Z 走 9 mm，循环就会插进 160 多个错误轴点，
    # 把整条中心轴拉成一条斜线（实测 bbox 被拉到 Y −1450 / Z −1320）。
    def _extend(seq, target_x, backwards: bool):
        out = list(seq)
        for _ in range(4):
            tail = np.array(out[0] if backwards else out[-1], dtype=float)
            if (tail[0] > target_x + 0.5) if backwards else (tail[0] < target_x - 0.5):
                ref = np.array(out[1] if backwards else out[-2], dtype=float)
                d = tail - ref if backwards else tail - ref
                n = np.linalg.norm(d)
                if n < 1e-6:
                    break
                d = d / n
                if d[0] < 0.2:              # 方向必须主要沿 X，否则退回 +X
                    d = np.array([1.0, 0.0, 0.0])
                d = d if backwards else d
                nxt = tail + (-1.0 if backwards else 1.0) * step * d
                if abs(nxt[1] - tail[1]) > 0.5 * step or abs(nxt[2] - tail[2]) > 0.5 * step:
                    break                    # Y/Z 跳变过大 ⇒ 方向不可信，停止外延
                if backwards:
                    out.insert(0, tuple(nxt))
                else:
                    out.append(tuple(nxt))
            else:
                break
        return out

    pts = _extend(pts, x1, backwards=False)
    pts = _extend(pts, x0, backwards=True)
    # 去重：追踪的头两步几乎重复（首步取到的形心 ≈ 起点本身）。若把重合点直接丢给
    # GeomAPI_PointsToBSpline，重复极点会生成巨大切向，整条中心轴在起点附近飞出去
    # （实测 P 跑到 Y=-2310 / Z=+1626，后续站位全部切不到实体）。
    clean = [pts[0]]
    for q in pts[1:]:
        if math.dist(q, clean[-1]) > 1e-2:
            clean.append(q)
    return clean


def build_spine(shape, x0, x1, n: int = 24):
    """拟合中心轴 BSpline。优先用追踪法（抗弯折），失败再退回 X 向形心法。"""
    try:
        pts = trace_spine(shape, x0, x1)
        src = "trace"
    except MoldGenerationError:
        pts = []
        for k in range(n):
            x = x0 + (x1 - x0) * (0.0005 + 0.999 * k / max(n - 1, 1))
            ws = section_wires_plane(shape, (x, 0.0, 0.0), (1.0, 0.0, 0.0))
            if len(ws) < 2:
                continue
            (wo, ao), (wi, ai) = ws[0], ws[1]
            pts.append(ring_centroid(wo, wi, ao, ai))
        src = "centroid"
        if len(pts) < 4:
            raise MoldGenerationError("中心轴拟合失败：有效截面太少", stage="corner_shrink")
    arr = TColgp_Array1OfPnt(1, len(pts))
    for i, p in enumerate(pts, 1):
        arr.SetValue(i, gp_Pnt(*p))
    mk = GeomAPI_PointsToBSpline(arr, 3, 8, GeomAbs_C2, 1e-3)
    if not mk.IsDone():
        raise MoldGenerationError("中心轴 BSpline 拟合失败", stage="corner_shrink")
    return mk.Curve(), pts, src


def spine_frames(curve, n: int):
    """沿 spine 取 n 个等弧长站位，返回 [(P, T), ...]。"""
    ad = BRepAdaptor_Curve(BRepBuilderAPI_MakeEdge(curve).Edge())
    d = GCPnts_QuasiUniformAbscissa(ad, n)
    if not d.IsDone():
        raise MoldGenerationError("中心轴采样失败", stage="corner_shrink")
    out = []
    for i in range(1, d.NbPoints() + 1):
        u = d.Parameter(i)
        P = curve.Value(u)
        h = 1e-3 * max((curve.LastParameter() - curve.FirstParameter()), 1e-6)
        p1 = curve.Value(max(u - h, curve.FirstParameter()))
        p2 = curve.Value(min(u + h, curve.LastParameter()))
        T = np.array([p2.X() - p1.X(), p2.Y() - p1.Y(), p2.Z() - p1.Z()])
        L = np.linalg.norm(T)
        if L < 1e-12:
            continue
        out.append((P, tuple(T / L)))
    return out


def _snap_axis(T, tol: float = 3e-3):
    """把接近坐标轴的切向吸附到该轴。

    这一步很关键：spine 拟合出的切向常带 0.001 rad 抖动，法向截面因此变成**斜切**，
    圆角被剖成椭圆/BSpline（类型变了就识别不到倒圆）。吸附后截面恢复成严格正切，
    角圆仍是精确的 Geom_Circle。
    """
    t = np.array(T, dtype=float)
    t /= max(np.linalg.norm(t), 1e-12)
    i = int(np.argmax(np.abs(t)))
    if abs(abs(t[i]) - 1.0) < tol:
        axis = np.zeros(3); axis[i] = math.copysign(1.0, t[i])
        return tuple(float(a) for a in axis)
    return tuple(float(a) for a in t)


def pick_profile_section(shape, frames, margin: float = 0.30):
    """挑一条能识别出倒圆的法向截面（优先靠近中段）。返回 (idx, (P,T), wires, r_thr)。"""
    order = sorted(range(len(frames)), key=lambda i: abs(i - len(frames) // 2))
    for i in order:
        P, T = frames[i]
        Ts = _snap_axis(T)
        ws = section_wires_plane(shape, (P.X(), P.Y(), P.Z()), Ts)
        if len(ws) < 2:
            continue
        r_thr = corner_radius_threshold(ws[0][1], margin)
        n_c = sum(1 for it in edge_items(ws[0][0])
                  if it["type"] == GeomAbs_Circle and it["R"] and it["R"] < r_thr)
        if n_c > 0:
            return i, (P, Ts), ws, r_thr
    return None, None, None, None


# =============================================================== 只读分析
def analyze(part_path: str, margin: float = 0.30, n_station: int = 12) -> dict:
    """分析数模：倒圆在哪、半径多少、真实截面是否恒定。"""
    product = read_product_model(part_path, verbose=False)
    bb = shape_bbox(product.shape)
    x0, x1 = bb[0], bb[3]

    # X 向截面（用于找倒圆、给前端展示）
    stations = []
    for k in range(n_station):
        x = x0 + (x1 - x0) * (0.04 + 0.92 * k / max(n_station - 1, 1))
        ws = section_wires_plane(product.shape, (x, 0.0, 0.0), (1.0, 0.0, 0.0))
        if len(ws) < 2:
            stations.append({"x": round(x, 2), "ok": False}); continue
        (wo, ao), (wi, _) = ws[0], ws[1]
        c = wire_center(wo)
        stations.append({"x": round(x, 2), "ok": True,
                         "perimeter": round(wire_len(wo), 4), "area": round(ao, 3),
                         "inner_perimeter": round(wire_len(wi), 4),
                         "cy": round(c.Y(), 4), "cz": round(c.Z(), 4)})
    good = [s for s in stations if s.get("ok")]
    if not good:
        raise MoldGenerationError("剖切不到有效截面（需要两个闭合环）", stage="corner_shrink")
    Ps = [s["perimeter"] for s in good]

    # 法向截面（沿 spine）—— 判断"真实截面"是否恒定
    curve, _, _src = build_spine(product.shape, x0, x1)
    frames = spine_frames(curve, max(n_station, 10))
    true_P = []
    for P, T in frames:
        ws = section_wires_plane(product.shape, (P.X(), P.Y(), P.Z()), T)
        if len(ws) < 2:
            continue
        true_P.append(wire_len(ws[0][0]))

    mid = good[len(good) // 2]
    ws = section_wires_plane(product.shape, (mid["x"], 0.0, 0.0), (1.0, 0.0, 0.0))
    (wo, ao), (wi, _) = ws[0], ws[1]
    frame = plane_basis((mid["x"], 0, 0), (1, 0, 0))
    r_thr = corner_radius_threshold(ao, margin)
    cor_o = [it["R"] for it in edge_items(wo)
             if it["type"] == GeomAbs_Circle and it["R"] and it["R"] < r_thr]
    cor_i = [it["R"] for it in edge_items(wi)
             if it["type"] == GeomAbs_Circle and it["R"] and it["R"] < r_thr]

    p_mean = float(np.mean(true_P)) if true_P else float(np.mean(Ps))
    var_true = (max(true_P) - min(true_P)) / p_mean * 100 if true_P else 999.0
    return {
        "file": os.path.abspath(part_path),
        "length_mm": round(x1 - x0, 3),
        "volume_mm3": round(product.volume, 2),
        "n_edges_outer": len(edge_items(wo)),
        "n_corners_outer": len(cor_o),
        "corner_radii_outer": sorted(round(r, 4) for r in cor_o),
        "n_corners_inner": len(cor_i),
        "corner_radii_inner": sorted(round(r, 4) for r in cor_i),
        "corner_threshold_mm": round(r_thr, 3),
        "perimeter_mm": round(p_mean, 4),
        "perimeter_var_pct": round(var_true, 4),
        "true_section_perimeters": [round(p, 4) for p in true_P],
        "prismatic": bool(var_true < 0.5),
        "stations": stations,
    }


# =============================================================== 生成
def shrink(part_path: str, dR: float = 2.0, output_dir: str = "./output",
           prefix: Optional[str] = None, n_station: int = 24, margin: float = 0.30,
           tol_pct: float = 0.5) -> dict:
    """生成"倒圆半径 −dR、各截面周长不变"的新管件。"""
    from naming_utils import part_number_from_filename
    from result_exporter import export_shapes

    info = analyze(part_path, margin=margin, n_station=max(n_station // 2, 10))
    if info["n_corners_outer"] == 0:
        raise MoldGenerationError(
            "未检测到明显倒圆角（阈值 R<%.2f mm）：本件截面没有可供缩小的角圆，按规则跳过。"
            % info["corner_threshold_mm"], stage="corner_shrink")

    product = read_product_model(part_path, verbose=False)
    bb = shape_bbox(product.shape)
    x0, x1 = bb[0], bb[3]
    curve, _, _src = build_spine(product.shape, x0, x1)
    frames = spine_frames(curve, n_station)

    # ---- 逐站法向截面重建 ------------------------------------------------------
    # 不用 BRepOffsetAPI_MakePipeShell：直段曲率为 0 时默认 Frenet 框架无定义（Build 失败）、
    # SetMode(gp_Ax2(...)) 在弯折处会把截面甩飞（实测 bbox 到 ±28 m）、SetMode(False) 直接失败。
    # 改成"每个站位切一个法向截面 → 就地重建 → 放样成壳"，端面直接用首尾两条重建截面做
    # 环形面，完全不需要推算扫掠框架。
    idx, (P_mid, T_mid), ws_mid, r_thr = pick_profile_section(
        product.shape, frames, margin)
    if idx is None:
        raise MoldGenerationError(
            "在法向截面上没能识别出倒圆角（可能角度太浅或截面为 BSpline）",
            stage="corner_shrink")

    def _annulus(wo, wi, tag):
        try:
            f = BRepBuilderAPI_MakeFace(TopoDS.Wire_s(wo), True).Face()
            return BRepBuilderAPI_MakeFace(f, TopoDS.Wire_s(wi)).Face()
        except Exception as exc:  # noqa: BLE001
            raise MoldGenerationError(f"{tag} 封口失败: {exc}", stage="corner_shrink") from exc

    # 壁厚 = 原件内外周长差 / 2π（本件 = 2.0 mm，且内外处处等距）
    thickness = max((wire_len(ws_mid[0][0]) - wire_len(ws_mid[1][0])) / (2.0 * math.pi), 0.2)

    t_now = time.perf_counter()
    outer_w, normals, sect_stats = [], [], []
    for (P, T) in frames:
        Ts = _snap_axis(T)
        ws = section_wires_plane(product.shape, (P.X(), P.Y(), P.Z()), Ts)
        if len(ws) < 2:
            continue
        fr = plane_basis((P.X(), P.Y(), P.Z()), Ts)
        try:
            wo2, s1, e1 = solve_section(ws[0][0], dR, r_thr, fr)
        except MoldGenerationError:
            continue
        if wo2 is None:
            continue
        c = wire_center(ws[0][0])
        wo_f = TopoDS.Wire_s(_scale_wire(wo2, s1, c))
        outer_w.append(normalize_wire(wo_f, Ts))
        normals.append(Ts)
        sect_stats.append({"p": [round(P.X(), 2), round(P.Y(), 2), round(P.Z(), 2)],
                           "scale": round(s1, 8), "dP_mm": round(float(e1), 8)})
    if len(outer_w) < 4:
        raise MoldGenerationError(
            "可重建的法向截面太少（%d 个）：可能管件太弯或端口处截面不完整" % len(outer_w),
            stage="corner_shrink")

    def _loft(wires, tag, solid=False):
        ts = BRepOffsetAPI_ThruSections(solid, False, 1e-6)
        ts.CheckCompatibility(False)
        for w in wires:
            ts.AddWire(w)
        ts.Build()
        if not ts.IsDone():
            raise MoldGenerationError(f"{tag} 放样失败", stage="corner_shrink")
        return ts.Shape()

    # 内孔 = 外轮廓在截面内**等距内偏一个壁厚**。
    # 这样壁厚与原件严格一致，内角半径自动 = 外角半径 − 壁厚 = (R−dR) − 壁厚，
    # 与"原件内角 − dR"完全吻合（原件内角 = 原件外角 − 壁厚）。
    inner_w = []
    for w, n in zip(outer_w, normals):
        inner_w.append(_offset_wire_in_plane(w, n, -thickness))

    shell_o = _loft(outer_w, "外轮廓")
    shell_i = _loft(inner_w, "内孔")
    cap0 = _annulus(outer_w[0], inner_w[0], "起点端面")
    cap1 = _annulus(outer_w[-1], inner_w[-1], "末端端面")

    sew = BRepBuilderAPI_Sewing(1e-4, True, True, True, False)
    for s_ in (shell_o, shell_i, cap0, cap1):
        sew.Add(s_)
    sew.Perform()
    sewn = sew.SewedShape()
    shells = []
    ex = TopExp_Explorer(sewn, TopAbs_SHELL)
    while ex.More():
        shells.append(TopoDS.Shell_s(ex.Current())); ex.Next()
    if not shells:
        raise MoldGenerationError("壳体缝合失败（没形成壳）", stage="corner_shrink")
    best, best_v = None, -1.0
    for sh in shells:
        try:
            v = abs(shape_volume(BRepBuilderAPI_MakeSolid(sh).Solid()))
        except Exception:  # noqa: BLE001
            v = 0.0
        if v > best_v:
            best, best_v = sh, v
    tube = BRepBuilderAPI_MakeSolid(best).Solid()
    t_total = time.perf_counter() - t_now
    if not is_valid_shape(tube, check_geometry=False):
        raise MoldGenerationError("重建后的管件实体无效", stage="corner_shrink")
    t_total = time.perf_counter() - t_now
    if not is_valid_shape(tube, check_geometry=False):
        raise MoldGenerationError("重建后的管件实体无效", stage="corner_shrink")

    vol = shape_volume(tube)
    # ---- 安全闸门：重建件的"外形胶囊"必须和原件一致 -------------------------------
    # 扫掠只能复现"沿中心轴等截面"的部分；若末端是弯头/斜切封口，中心轴会从形心漂出去，
    # 扫出来的长度、Y/Z 轮廓会明显偏离。这种情况**必须报错**，绝不能静默交出错件。
    bb_src = shape_bbox(product.shape)
    bb_new = shape_bbox(tube)
    len_src = bb_src[3] - bb_src[0]
    len_new = bb_new[3] - bb_new[0]
    d_len = len_new - len_src
    dy = (bb_new[4] - bb_new[1]) - (bb_src[4] - bb_src[1])
    dz = (bb_new[5] - bb_new[2]) - (bb_src[5] - bb_src[2])
    tol_len = max(0.005 * len_src, 1.0)
    tol_side = max(0.02 * max(bb_src[4] - bb_src[1], bb_src[5] - bb_src[2]), 1.0)
    if abs(d_len) > tol_len or abs(dy) > tol_side or abs(dz) > tol_side:
        raise MoldGenerationError(
            "重建后的外形与原件不符（长度 %+.2f mm、ΔY %+.2f mm、ΔZ %+.2f mm，"
            "容差 ±%.1f / ±%.1f mm）：本件的末端是弯头/斜切封口，"
            "用\"环形截面形心\"拟合的中心轴没能跟住真实轴线。"
            "这种件需要改用以角圆面自身脊线为轴的重建方式（尚未实现），"
            "或改为只替换倒圆面、其余面原样保留。"
            % (d_len, dy, dz, tol_len, tol_side),
            stage="corner_shrink")

    name = prefix or part_number_from_filename(part_path)
    out_dir = os.path.join(output_dir, f"{name}_dR{dR:g}")
    paths = export_shapes({"corner_shrunk": tube}, output_dir=out_dir, prefix=name,
                          metadata={
                              "op": "corner_shrink", "source": os.path.abspath(part_path),
                              "dR_mm": dR,
                              "corner_before_mm": info["corner_radii_outer"],
                              "corner_after_mm": info["corner_radii_inner"],
                              "perimeter_before_mm": info["perimeter_mm"],
                              "scale": s, "dp_residual_mm": err,
                              "volume_before_mm3": info["volume_mm3"],
                              "volume_after_mm3": round(vol, 3),
                              "sections_n": len(frames),
                              "prismatic_true_section": info["prismatic"],
                          })
    return {
        "ok": True, "name": name,
        "step_path": paths.get("corner_shrunk"),
        "metadata_path": paths.get("metadata"),
        "volume_before_mm3": info["volume_mm3"], "volume_after_mm3": vol,
        "volume_change_pct": round((vol - info["volume_mm3"]) / info["volume_mm3"] * 100, 4),
        "corner_radii_before_mm": info["corner_radii_outer"],
        "corner_radii_after_mm": info["corner_radii_inner"],
        "perimeter_before_mm": info["perimeter_mm"],
        "perimeter_after_mm": round(info["perimeter_mm"] + err, 4),
        "scale": round(s, 8), "dp_residual_mm": err,
        "sections_used": len(frames),
        "bbox": list(shape_bbox(tube)),
        "analysis": info,
    }


if __name__ == "__main__":
    import json
    import sys

    from io_utils import ensure_utf8_stdout

    ensure_utf8_stdout()
    if len(sys.argv) < 2:
        print("用法: python corner_shrink.py <part.stp> [dR] [--analyze]")
        raise SystemExit(1)
    path = sys.argv[1]
    nums = [a for a in sys.argv[2:] if not a.startswith("--")]
    dR = float(nums[0]) if nums else 2.0
    if "--analyze" in sys.argv:
        print(json.dumps(analyze(path), ensure_ascii=False, indent=2, default=str))
    else:
        r = shrink(path, dR=dR)
        print(json.dumps({k: v for k, v in r.items() if k != "analysis"},
                         ensure_ascii=False, indent=2, default=str))
