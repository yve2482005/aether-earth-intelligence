"""AETHER gis helpers — minimal GeoJSON builders for backend use.

Stdlib only. Produces RFC 7946 FeatureCollections for map layers
(earthquake points, tsunami zones, weather grid points).
"""
import json


def point(lon, lat, props=None):
    """A single GeoJSON Point feature."""
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [float(lon), float(lat)]},
        "properties": dict(props or {}),
    }


def polygon(rings, props=None):
    """A GeoJSON Polygon feature. rings = list of [[lon, lat], ...] (first = outer)."""
    return {
        "type": "Feature",
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[float(x), float(y)] for x, y in ring] for ring in rings],
        },
        "properties": dict(props or {}),
    }


def feature_collection(features, props=None):
    """Wrap features in a FeatureCollection; optional collection-level properties."""
    fc = {"type": "FeatureCollection", "features": list(features)}
    if props:
        fc["properties"] = dict(props)
    return fc


def bbox_of(fc):
    """[min_lon, min_lat, max_lon, max_lat] over all Point/Polygon coords, or None."""
    xs, ys = [], []

    def walk(coords):
        if isinstance(coords[0], (int, float)):
            xs.append(coords[0])
            ys.append(coords[1])
        else:
            for c in coords:
                walk(c)

    for f in fc.get("features", []):
        g = f.get("geometry") or {}
        if g.get("coordinates"):
            walk(g["coordinates"])
    if not xs:
        return None
    return [min(xs), min(ys), max(xs), max(ys)]


def write_geojson(path, fc):
    """Write a FeatureCollection to path (UTF-8 JSON). Returns the path."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(fc, fh, ensure_ascii=False)
    return path


def quake_feature(quake):
    """Normalize a backend quake dict -> GeoJSON Point with AETHER properties."""
    q = quake or {}
    props = {
        "id": q.get("id"),
        "magnitude": q.get("magnitude", q.get("mag")),
        "depth_km": q.get("depth_km", q.get("depth")),
        "time": q.get("time", q.get("occurred_at")),
        "region": q.get("region", q.get("place")),
        "kind": "detection",
        "source": q.get("source", q.get("agency")),
    }
    return point(q.get("lon", q.get("longitude")), q.get("lat", q.get("latitude")), props)
