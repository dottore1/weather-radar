"""One-off script: build a detailed-but-lightweight Denmark-area coastline
from Natural Earth's public-domain 10m admin-0-countries dataset, clipped to
our display bbox and simplified for embedding as inline SVG polygons in
dev/index.html.

Not part of the runtime pipeline -- shapely is a throwaway dependency for
this generation step only, not added to dev/requirements.txt (pip install
shapely into the dev venv to re-run this).

To reproduce: download the source data (public domain, CC0, no attribution
required) and place it at dev/_scratch/ne_10m_countries.geojson:

    curl -sL -o dev/_scratch/ne_10m_countries.geojson \
      https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_10m_admin_0_countries.geojson

(Note: the sibling file ne_10m_land.geojson on that same mirror is NOT
usable for this -- it turned out to be an oddly pre-merged ~11-feature
version that fuses Scandinavia into mainland Europe with no sea gap.
admin_0_countries has proper per-country topology.)
"""
import json

from shapely.geometry import shape, box
from shapely.ops import unary_union

LON_MIN, LON_MAX, LAT_MIN, LAT_MAX = 7.0, 16.0, 54.3, 58.2
SIMPLIFY_TOLERANCE_DEG = 0.006  # ~ 0.7px at our ~1000px/9deg display scale

with open("dev/_scratch/ne_10m_countries.geojson", encoding="utf-8") as f:
    data = json.load(f)

geoms = [shape(feat["geometry"]) for feat in data["features"]]
land = unary_union(geoms)

clip_box = box(LON_MIN, LAT_MIN, LON_MAX, LAT_MAX)
clipped = land.intersection(clip_box)
simplified = clipped.simplify(SIMPLIFY_TOLERANCE_DEG, preserve_topology=True)

polys = list(simplified.geoms) if simplified.geom_type == "MultiPolygon" else [simplified]

# Drop slivers too small to matter (keeps the output lean) and sort largest
# first so bigger landmasses paint before small islands, if that ever matters.
polys = [p for p in polys if p.area > 1e-5]
polys.sort(key=lambda p: -p.area)

total_pts = 0
js_polys = []
for p in polys:
    coords = list(p.exterior.coords)
    total_pts += len(coords)
    ring = "[" + ",".join(f"[{lon:.4f},{lat:.4f}]" for lon, lat in coords) + "]"
    js_polys.append(ring)
    for interior in p.interiors:  # holes (e.g. large inland lakes) — keep if any survive simplification
        icoords = list(interior.coords)
        if len(icoords) >= 4:
            total_pts += len(icoords)

print(f"{len(polys)} polygons, {total_pts} points total")

with open("dev/_scratch/coastline_js.txt", "w", encoding="utf-8") as f:
    f.write("[\n " + ",\n ".join(js_polys) + "\n]\n")

print("wrote dev/_scratch/coastline_js.txt")
