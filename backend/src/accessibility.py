"""Walking distance along real streets and paths (OpenStreetMap + OSMnx).

Straight-line buffers overstate access where railway lines, creeks or arterial roads block a
direct walk. Every lot gets the shortest walking-network distance to the nearest destination of
each kind (station, supermarket, pharmacy ...) using one multi-source Dijkstra per kind.
"""
from __future__ import annotations

import heapq
import os
import time
from collections import defaultdict

import geopandas as gpd
import networkx as nx
import numpy as np
import pandas as pd

from . import config as C

GRAPH_PATH = C.RAW / "walk_network.graphml"


def load_walk_graph(boundary_metric) -> nx.MultiGraph:
    """Download (once) the pedestrian network for the council plus a 1.5 km margin."""
    import osmnx as ox

    # osmnx caches downloads in ./cache by default, which isn't writable inside the container.
    ox.settings.cache_folder = str(C.DATA_DIR / "cache" / "osmnx")
    ox.settings.log_file = False
    if GRAPH_PATH.exists():
        G = ox.load_graphml(GRAPH_PATH)
    else:
        poly = gpd.GeoSeries([boundary_metric.buffer(1500)], crs=C.CRS_METRIC).to_crs(C.CRS_WGS84).iloc[0]
        G, errors = None, []
        ox.settings.requests_timeout = 300
        for url in C.OVERPASS_API_BASES:          # public Overpass servers are often busy; try mirrors
            for attempt in range(2):
                try:
                    ox.settings.overpass_url = url
                    print(f"  downloading walking network from {url} (a few minutes)...")
                    G = ox.graph_from_polygon(poly, network_type="walk", simplify=True)
                    break
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{url}: {str(e)[:160]}")
                    print(f"    failed ({str(e)[:120]}); " + ("retrying in 30 s" if attempt == 0 else "trying next server"))
                    if attempt == 0:
                        time.sleep(30)
            if G is not None:
                break
        if G is None:
            raise RuntimeError("all Overpass servers failed: " + " | ".join(errors))
        tmp = GRAPH_PATH.with_suffix(".graphml.tmp")
        ox.save_graphml(G, tmp)
        os.replace(tmp, GRAPH_PATH)
    G = ox.project_graph(G, to_crs=C.CRS_METRIC)
    return ox.convert.to_undirected(G)


class WalkNetwork:
    """A walking graph prepared for repeated 'nearest destination' queries."""

    def __init__(self, G: nx.MultiGraph):
        self.adj: dict = defaultdict(dict)
        for u, v, d in G.edges(data=True):
            L = float(d.get("length", 0.0))
            if L < self.adj[u].get(v, np.inf):
                self.adj[u][v] = L
                self.adj[v][u] = L
        nodes = pd.DataFrame([(n, d["x"], d["y"]) for n, d in G.nodes(data=True)], columns=["node", "x", "y"])
        self.nodes = gpd.GeoDataFrame(nodes[["node"]], geometry=gpd.points_from_xy(nodes.x, nodes.y), crs=C.CRS_METRIC)
        self.n_edges = G.number_of_edges()

    def snap(self, geoms: gpd.GeoSeries) -> tuple[np.ndarray, np.ndarray]:
        """Nearest graph node and the straight-line distance to it, for each geometry."""
        g = gpd.GeoDataFrame(geometry=list(geoms.values), crs=C.CRS_METRIC)
        j = gpd.sjoin_nearest(g, self.nodes, distance_col="snap_m")
        j = j[~j.index.duplicated()].sort_index()
        return j["node"].to_numpy(), j["snap_m"].to_numpy()

    def _dijkstra(self, sources: list[tuple[float, object, str]], cutoff: float) -> dict:
        """Multi-source Dijkstra: node -> (distance, name of the source that reaches it first)."""
        best: dict = {}
        heap = [(d, i, n, name) for i, (d, n, name) in enumerate(sources)]
        heapq.heapify(heap)
        counter = len(heap)
        while heap:
            d, _, node, name = heapq.heappop(heap)
            if node in best or d > cutoff:
                continue
            best[node] = (d, name)
            for nb, L in self.adj[node].items():
                nd = d + L
                if nb not in best and nd <= cutoff:
                    counter += 1
                    heapq.heappush(heap, (nd, counter, nb, name))
        return best

    def nearest(self, targets: gpd.GeoDataFrame, name_col: str, p_nodes: np.ndarray, p_snap: np.ndarray,
                cutoff: float) -> tuple[np.ndarray, np.ndarray]:
        """Distance (m) and name of the nearest target for already-snapped points (NaN/None beyond cutoff)."""
        if targets is None or len(targets) == 0:
            return np.full(len(p_nodes), np.nan), np.full(len(p_nodes), None, dtype=object)
        t_nodes, t_snap = self.snap(targets.geometry)
        best = self._dijkstra(list(zip(t_snap.astype(float), t_nodes, targets[name_col].astype(str))), cutoff)
        dist = np.array([best[n][0] if n in best else np.nan for n in p_nodes]) + p_snap
        names = np.array([best[n][1] if n in best else None for n in p_nodes], dtype=object)
        over = ~(dist <= cutoff)
        dist[over], names[over] = np.nan, None
        return np.round(dist, 0), names


def walk_distances(G: nx.MultiGraph, stations: gpd.GeoDataFrame, points: gpd.GeoSeries,
                   net: WalkNetwork | None = None) -> pd.DataFrame:
    """Walking distance (m) from each point to its nearest station along graph G."""
    net = net or WalkNetwork(G)
    p_nodes, p_snap = net.snap(points)
    d, names = net.nearest(stations, "stop_name", p_nodes, p_snap, C.WALK_MAX_M)
    out = pd.DataFrame({"walk_m": d, "walk_station": names}, index=points.index)
    out["walk_min"] = (out["walk_m"] / C.WALK_SPEED_M_PER_MIN).round(1)
    return out
